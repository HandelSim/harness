// npm test: bundle every src/**/*.test.ts with esbuild to out/test/**/*.test.mjs and run them with node:test.
// Args: file filters (substring match on the source path), e.g. `node scripts/run-tests.mjs main/backend`.
import * as esbuild from 'esbuild';
import { readdirSync, rmSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const desktop = join(dirname(fileURLToPath(import.meta.url)), '..');
const filters = process.argv.slice(2).filter((a) => !a.startsWith('--'));
function walk(d, out = []) {
  for (const n of readdirSync(d)) {
    const p = join(d, n);
    if (statSync(p).isDirectory()) walk(p, out); else if (n.endsWith('.test.ts')) out.push(p);
  }
  return out;
}
const files = walk(join(desktop, 'src')).filter((f) => !filters.length || filters.some((x) => f.includes(x)));
if (!files.length) { console.log('no test files'); process.exit(0); }
const outdir = join(desktop, 'out', 'test');
rmSync(outdir, { recursive: true, force: true });
await esbuild.build({ entryPoints: files, outdir, outbase: join(desktop, 'src'), outExtension: { '.js': '.mjs' }, bundle: true,
  platform: 'node', format: 'esm', target: 'node22', sourcemap: 'inline', logLevel: 'warning', external: ['electron', 'node-pty'],
  define: { __DEV__: 'true' }, loader: { '.glsl': 'text' },
  banner: { js: "import { createRequire as __cr } from 'node:module'; const require = __cr(import.meta.url);" } });
const outs = files.map((f) => join(outdir, relative(join(desktop, 'src'), f)).replace(/\.ts$/, '.mjs'));
const r = spawnSync(process.execPath, ['--test', '--test-reporter=spec', ...outs], { stdio: 'inherit', cwd: desktop });
process.exit(r.status ?? 1);
