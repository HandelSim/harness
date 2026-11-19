// M0 boot: the app attaches to the mock serve and reaches data-ready (or the window load before L3 mounts).
import { test, expect, mainHooks, waitReady } from '../fixtures';

test('boots against the mock serve and the backend comes up', async ({ app, win, mock }) => {
  const how = await waitReady(win);
  expect(win.url()).toBe('app://harness/index.html');
  await expect.poll(() => mainHooks(app, (h) => h.backend.get().phase), { timeout: 15000 }).toBe('up');
  const reqs = await mock.requests('/api/v1/status');
  expect(reqs.length).toBeGreaterThan(0);
  // main's node:http requests carry no browser headers
  expect(reqs[0].headers.origin).toBeUndefined();
  expect(reqs[0].headers['sec-fetch-site']).toBeUndefined();
  test.info().annotations.push({ type: 'ready', description: how });
});

test('the preload exposes the whole bridge and nothing else', async ({ win }) => {
  await waitReady(win);
  const r = await win.evaluate(() => {
    const w = window as any;
    return { harness: typeof w.harness, require: typeof w.require, process: typeof w.process, keys: Object.keys(w.harness || {}).sort() };
  });
  expect(r.harness).toBe('object');
  expect(r.require).toBe('undefined');
  expect(r.process).toBe('undefined');
  for (const k of ['app', 'backend', 'api', 'live', 'harness', 'keys', 'settings', 'state', 'pty', 'web', 'update', 'shell', 'window', 'notify', 'log', 'on']) {
    expect(r.keys, `bridge member ${k}`).toContain(k);
  }
});
