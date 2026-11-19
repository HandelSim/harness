// Packaged smoke: node scripts/smoke-packaged.mjs [path-to-unpacked-binary] (from desktop/, under xvfb-run -a on a headless box)
// after npm run dist:dir. Starts the mock serve, launches the packaged app, checks data-ready, the bridge, #bg-root and a real pty.
// Packaged smoke over CDP (the inspect fuse is off, so Playwright's _electron cannot attach by design).
import { chromium } from '@playwright/test';
import { spawn } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
const D = process.cwd();
const { startMock } = await import(join(D, 'e2e/mock-serve/server.mjs'));
const mock = await startMock({ scenario: 'healthy' });
const ud = mkdtempSync(join(tmpdir(), 'harness-smoke-'));
const port = 20000 + Math.floor(Math.random() * 20000);
const env = { ...process.env, HARNESS_DESKTOP_USER_DATA: ud, HARNESS_DESKTOP_NO_UPDATE: '1', HARNESS_DESKTOP_SERVE_URL: mock.url };
delete env.ELECTRON_RUN_AS_NODE;
const exe = resolve(D, process.argv[2] || `dist/linux-${process.arch}-unpacked/harness-desktop`);
const t0 = Date.now();
const child = spawn(exe, ['--no-sandbox', `--remote-debugging-port=${port}`, '--remote-debugging-address=127.0.0.1', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'], { env, stdio: ['ignore', 'pipe', 'pipe'] });
let log = ''; child.stdout.on('data', (d) => { log += d; }); child.stderr.on('data', (d) => { log += d; });
const ok = (m) => console.log('SMOKE OK:', m);
let browser;
try {
  for (let i = 0; i < 60 && !browser; i++) { try { browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`); } catch { await new Promise((r) => setTimeout(r, 500)); } }
  if (!browser) throw new Error('no CDP endpoint');
  let page;
  for (let i = 0; i < 60 && !page; i++) { page = browser.contexts().flatMap((c) => c.pages()).find((p) => !p.url().startsWith('devtools')); if (!page) await new Promise((r) => setTimeout(r, 500)); }
  if (!page) throw new Error('no window');
  ok(`window url=${page.url().replace(/\?.*/, '')}`);
  await page.waitForSelector('html[data-ready="1"]', { timeout: 30000 });
  ok(`data-ready in ${Date.now() - t0} ms`);
  const info = await page.evaluate(async () => ({ bg: !!document.getElementById('bg-root'), bridge: typeof window.harness, tabs: document.querySelectorAll('.dv-tab').length }));
  ok(`bridge=${info.bridge} bg-root=${info.bg} tabs=${info.tabs}`);
  const r = await page.evaluate(async () => window.harness.pty.spawn({ kind: 'plain', cwd: '/tmp' }, 80, 24));
  if (!r || !r.ok) throw new Error('pty spawn failed: ' + JSON.stringify(r));
  ok(`pty spawned (node-pty loads from app.asar.unpacked)`);
  await page.evaluate((id) => window.harness.pty.kill(id), r.id).catch(() => {});
  await page.screenshot({ path: process.env.SMOKE_SHOT || join(ud, 'smoke.png') });
  ok('screenshot saved');
  console.log('SMOKE PASS');
} catch (e) { console.log('SMOKE FAIL:', e.message); console.log(log.slice(-1500)); process.exitCode = 1; }
finally { try { await browser?.close(); } catch {} child.kill('SIGTERM'); setTimeout(() => child.kill('SIGKILL'), 3000).unref(); await mock.close(); }
