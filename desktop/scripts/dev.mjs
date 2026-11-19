// Dev loop: esbuild in watch mode, Electron restarted when main, preload or the PTY host change.
// Renderer changes land in out/renderer; reload the window (Ctrl+R) to pick them up.
import { spawn } from 'node:child_process';
import { existsSync, watch } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const electron = createRequire(import.meta.url)('electron'); // the binary path
const extra = process.argv.slice(2);
if (process.platform === 'linux' && process.env.HARNESS_DESKTOP_DEV_SANDBOX !== '1') extra.push('--no-sandbox');

const builder = spawn(process.execPath, [join(root, 'scripts/build.mjs'), '--watch'], { stdio: ['ignore', 'pipe', 'inherit'] });
let app = null;
let restarting = false;
let stopping = false;

function launch() {
  app = spawn(electron, [root, ...extra], { stdio: 'inherit', env: { ...process.env, HARNESS_DESKTOP_DEV: '1' } });
  app.on('exit', (code) => {
    app = null;
    if (restarting) { restarting = false; launch(); return; }
    if (!stopping) shutdown(code ?? 0); // the window was closed: end the dev loop
  });
}

function restart() {
  if (!app) return launch();
  restarting = true;
  app.kill('SIGTERM');
  setTimeout(() => { if (app && restarting) app.kill('SIGKILL'); }, 5000).unref();
}

function shutdown(code) {
  stopping = true;
  if (app) app.kill('SIGTERM');
  builder.kill('SIGTERM');
  process.exit(code);
}
process.on('SIGINT', () => shutdown(130));
process.on('SIGTERM', () => shutdown(143));

let timer = null;
const later = () => { clearTimeout(timer); timer = setTimeout(restart, 300); };
let started = false;
builder.stdout.setEncoding('utf8');
builder.stdout.on('data', (s) => {
  process.stdout.write(s);
  if (started || !existsSync(join(root, 'out/main/index.cjs'))) return;
  if (!/built main/.test(s) && !existsSync(join(root, 'out/preload/index.cjs'))) return;
  started = true;
  setTimeout(() => {
    launch();
    for (const d of ['out/main', 'out/preload']) watch(join(root, d), later);
  }, 500);
});
builder.on('exit', (code) => { if (!stopping) { console.error(`build watcher exited (${code})`); shutdown(1); } });
