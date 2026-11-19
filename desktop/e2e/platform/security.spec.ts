// Renderer hardening: no navigation, no popups, IPC payload checks, no direct network.
import { test, expect, mainHooks, waitReady } from '../fixtures';

test('navigation, popups and foreign URLs are refused', async ({ app, win }) => {
  await waitReady(win);
  await win.evaluate(() => { window.open('https://example.invalid/', '_blank'); });
  await win.evaluate(() => { location.href = 'https://example.invalid/'; }).catch(() => {});
  await win.waitForTimeout(800);
  // top-level windows only (a <webview> guest of the Web panel is a Playwright page too)
  const urls = await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().map((w) => w.webContents.getURL()));
  expect(urls).toEqual(['app://harness/index.html']);
  expect(win.url()).toBe('app://harness/index.html');
});

test('IPC payloads are validated', async ({ app, win, mock }) => {
  await waitReady(win);
  await expect.poll(() => mainHooks(app, (h) => h.backend.get().phase), { timeout: 15000 }).toBe('up');
  const r = await win.evaluate(async () => {
    const h = window.harness;
    const trav = await h.api.get('/api/v1/../../etc/passwd');
    const foreign = await h.api.get('/login');
    const okq = await h.api.get('/api/v1/queue');
    const ext1 = await h.shell.openExternal('file:///etc/passwd');
    const ext2 = await h.shell.openExternal('https://example.com/');
    const sec = await h.harness.configSet('PROXY_API_KEY', 'sk-123');
    return { trav: trav.ok, foreign: foreign.ok, okq: okq.ok, ext1, ext2, sec: sec.ok };
  });
  expect(r).toEqual({ trav: false, foreign: false, okq: true, ext1: false, ext2: true, sec: false });
  const paths = (await mock.requests()).map((x) => x.path);
  expect(paths.some((p) => p.includes('passwd') || p === '/login')).toBe(false);
});

test('a session cookie from a codes-on serve stays in main', async ({ launch, mock }) => {
  await mock.scenario('codes-on');
  const { app, win } = await launch();
  await waitReady(win);
  await expect.poll(() => mainHooks(app, (h) => h.backend.get().phase), { timeout: 15000 }).toBe('needs-login');
  const code = await mock.accessCode();
  const st = await win.evaluate((c) => window.harness.backend.action({ action: 'login', code: c }), code);
  expect(st.phase).toBe('up');
  expect(await win.evaluate(() => document.cookie)).toBe('');
  const q = await win.evaluate(() => window.harness.api.get('/api/v1/queue'));
  expect(q.ok).toBe(true);
});
