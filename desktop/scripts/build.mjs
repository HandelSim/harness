// Build every bundle with the esbuild API in one process (PLAN 6.3).
// Missing entries owned by other legs are skipped with a warning in dev and are fatal with --prod.
import * as esbuild from 'esbuild';
import { existsSync, mkdirSync, copyFileSync, rmSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const args = new Set(process.argv.slice(2));
const prod = args.has('--prod');
const watch = args.has('--watch');
const r = (p) => join(root, p);

const nodeCommon = {
  bundle: true, platform: 'node', format: 'cjs', target: 'node24',
  sourcemap: prod ? false : 'inline', minify: prod, logLevel: 'warning',
  define: { __DEV__: String(!prod) },
};

const targets = [
  { name: 'main', entry: 'src/main/index.ts', opts: { ...nodeCommon, outfile: r('out/main/index.cjs'),
    external: ['electron', 'node-pty', 'electron-updater'] } },
  { name: 'pty-host', entry: 'src/main/pty/host.ts', opts: { ...nodeCommon, outfile: r('out/main/pty-host.cjs'),
    external: ['node-pty'] } },
  { name: 'preload', entry: 'src/preload/index.ts', opts: { ...nodeCommon, platform: 'browser', format: 'cjs',
    target: 'chrome156', outfile: r('out/preload/index.cjs'), external: ['electron'] } },
  { name: 'renderer', entry: 'src/renderer/main.tsx', opts: { bundle: true, platform: 'browser', format: 'esm',
    splitting: true, target: 'chrome156', jsx: 'automatic', jsxImportSource: 'preact', outdir: r('out/renderer/assets'),
    entryNames: '[name]', chunkNames: 'chunk-[hash]', assetNames: '[name]-[hash]',
    loader: { '.woff2': 'file', '.woff': 'file', '.glsl': 'text', '.svg': 'file', '.png': 'file' },
    sourcemap: prod ? false : true, minify: prod, logLevel: 'warning', define: { __DEV__: String(!prod) } },
    copy: [['src/renderer/index.html', 'out/renderer/index.html']] },
  { name: 'bg-worker', entry: 'src/background/worker.ts', opts: { bundle: true, platform: 'browser', format: 'esm',
    target: 'chrome156', outfile: r('out/renderer/assets/bg-worker.js'), loader: { '.glsl': 'text' },
    sourcemap: prod ? false : true, minify: prod, logLevel: 'warning', define: { __DEV__: String(!prod) } } },
];
if (!prod) targets.push({ name: 'bg-demo', entry: 'src/background/demo.ts', opts: { bundle: true, platform: 'browser',
  format: 'esm', target: 'chrome156', outfile: r('out/renderer/assets/bg-demo.js'), loader: { '.glsl': 'text' },
  sourcemap: true, logLevel: 'warning', define: { __DEV__: 'true' } },
  copy: [['src/background/demo.html', 'out/renderer/bg-demo.html']] });

export async function build({ quiet = false } = {}) {
  if (!watch) rmSync(r('out/main'), { recursive: true, force: true });
  const missing = [];
  const jobs = [];
  for (const t of targets) {
    if (!existsSync(r(t.entry))) { missing.push(t.entry); continue; }
    for (const [from, to] of t.copy || []) {
      if (!existsSync(r(from))) { missing.push(from); continue; }
      mkdirSync(dirname(r(to)), { recursive: true });
      copyFileSync(r(from), r(to));
    }
    const name = t.name;
    jobs.push((async () => {
      const t0 = Date.now();
      if (watch) {
        const ctx = await esbuild.context({ ...t.opts, entryPoints: [r(t.entry)] });
        await ctx.watch();
      } else {
        await esbuild.build({ ...t.opts, entryPoints: [r(t.entry)] });
      }
      if (!quiet) console.log(`built ${t.name} in ${Date.now() - t0} ms`);
    })().catch((e) => { throw Object.assign(new Error(`${name}: ${e?.message || e}`), { target: name }); }));
  }
  if (missing.length) {
    const msg = `missing entries (other legs): ${missing.join(', ')}`;
    if (prod) { console.error(`FATAL ${msg}`); process.exit(1); }
    console.warn(`warning: ${msg}`);
  }
  const results = await Promise.allSettled(jobs);
  const failed = results.filter((x) => x.status === 'rejected');
  // main, preload and the pty host (this leg) must build; in dev a broken renderer/background entry only warns,
  // so one leg's work in progress does not stop the others' e2e runs.
  const CORE = new Set(['main', 'pty-host', 'preload']);
  const fatal = failed.filter((f) => prod || CORE.has(f.reason?.target));
  for (const f of failed) console.error(`${fatal.includes(f) ? 'ERROR' : 'warning (dev, skipped)'}: ${String(f.reason?.message || f.reason)}`);
  if (fatal.length && !watch) process.exit(1);
  // No renderer yet (L3 not landed): a placeholder page so the window still loads in dev.
  if (!existsSync(r('out/renderer/index.html')) && !prod) {
    mkdirSync(r('out/renderer'), { recursive: true });
    const { writeFileSync } = await import('node:fs');
    writeFileSync(r('out/renderer/index.html'),
      '<!doctype html><html><head><meta charset="utf-8"><title>Harness</title></head>' +
      '<body><div id="bg-root"></div><p>renderer not built yet</p></body></html>\n');
  }
}

if (import.meta.url === `file://${process.argv[1]}`) await build();
