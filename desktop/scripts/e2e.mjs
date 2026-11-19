// npm run e2e: build, then Playwright (Electron) under xvfb-run when there is no display. Extra args go to playwright.
import { spawnSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { build } from './build.mjs';

const desktop = join(dirname(fileURLToPath(import.meta.url)), '..');
if (!process.argv.includes('--no-build')) await build();
const args = process.argv.slice(2).filter((a) => a !== '--no-build');
const pw = [join(desktop, 'node_modules', '@playwright', 'test', 'cli.js'), 'test', ...args];
const needX = process.platform === 'linux' && !process.env.DISPLAY && !process.env.WAYLAND_DISPLAY;
const cmd = needX ? 'xvfb-run' : process.execPath;
const argv = needX ? ['-a', '-s', '-screen 0 1600x1000x24', process.execPath, ...pw] : pw;
const r = spawnSync(cmd, argv, { cwd: desktop, stdio: 'inherit', env: process.env });
if (r.error) { console.error(`e2e: ${r.error.message}${needX ? ' (is xvfb-run installed?)' : ''}`); process.exit(1); }
process.exit(r.status ?? 1);
