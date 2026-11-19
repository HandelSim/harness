// Renderer unit tests: bundle each src/renderer/**/*.test.ts with esbuild into a temp dir, then run node --test.
// Usage (from desktop/): node src/renderer/test/run.mjs [filter]
import { build } from 'esbuild';
import { readdirSync, statSync, mkdtempSync, rmSync } from 'node:fs';
import { join, relative } from 'node:path';
import { tmpdir } from 'node:os';
import { spawnSync } from 'node:child_process';

const root = new URL('..', import.meta.url).pathname;
const filter = process.argv[2] ?? '';
const files = [];
(function walk(d) {
  for (const n of readdirSync(d)) {
    const p = join(d, n);
    if (statSync(p).isDirectory()) walk(p);
    else if (/\.test\.tsx?$/.test(n) && p.includes(filter)) files.push(p);
  }
})(root);
const out = mkdtempSync(join(process.env.TEST_TMP || tmpdir(), 'rtest-'));
try {
  await build({ entryPoints: files, bundle: true, platform: 'node', format: 'esm', outdir: out, outbase: root,
    jsx: 'automatic', jsxImportSource: 'preact', define: { __DEV__: 'true' }, logLevel: 'warning',
    loader: { '.css': 'empty' }, external: ['node:*'], outExtension: { '.js': '.mjs' } });
  const outs = files.map((f) => join(out, relative(root, f)).replace(/\.tsx?$/, '.mjs'));
  const r = spawnSync(process.execPath, ['--test', ...outs], { stdio: 'inherit' });
  process.exitCode = r.status ?? 1;
} finally { rmSync(out, { recursive: true, force: true }); }
