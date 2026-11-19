// Backend manager against the fake harness: spawn an owned serve, needs-config, locked key, quit stops it.
import { test, expect, mainHooks, waitReady, fakeState } from '../fixtures';
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';

const phase = (app: Parameters<typeof mainHooks>[0]) => mainHooks(app, (h) => h.backend.get().phase);

test('spawns an owned serve on the fake install and stops it on quit', async ({ launch }) => {
  const { app, win, root } = await launch({ fakeHarness: {} });
  await waitReady(win);
  await expect.poll(() => phase(app), { timeout: 30000 }).toBe('up');
  const st = await win.evaluate(() => window.harness.backend.get());
  expect(st.owned).toBe(true);
  expect(st.url).toMatch(/^http:\/\/127\.0\.0\.1:\d+$/);
  const calls = fakeState(root!).calls.map((c) => c.join(' '));
  expect(calls.some((c) => c === 'config list --tsv')).toBe(true);
  expect(calls.some((c) => c.startsWith('serve --port'))).toBe(true);
  const pid = Number(readFileSync(join(root!, 'state', 'fake-serve.pid'), 'utf8'));
  await win.evaluate(() => window.harness.backend.action({ action: 'stop' }));
  await expect.poll(() => { try { process.kill(pid, 0); return 'alive'; } catch { return 'gone'; } }, { timeout: 15000 }).toBe('gone');
});

test('no .env -> needs-config; create-env copies .env.example', async ({ launch }) => {
  const { app, win, root } = await launch({ fakeHarness: { env: 'missing' } });
  await waitReady(win);
  await expect.poll(() => phase(app), { timeout: 15000 }).toBe('needs-config');
  expect((await win.evaluate(() => window.harness.backend.get())).missing).toContain('.env');
  await win.evaluate(() => window.harness.backend.action({ action: 'create-env' }));
  expect(existsSync(join(root!, '.env'))).toBe(true);
});

test('empty required key -> needs-config with the key named', async ({ launch }) => {
  const { app, win } = await launch({ fakeHarness: { env: 'empty-key' } });
  await waitReady(win);
  await expect.poll(() => phase(app), { timeout: 15000 }).toBe('needs-config');
  expect((await win.evaluate(() => window.harness.backend.get())).missing).toContain('PROXY_API_KEY');
});

test('a locked key restarts serve with the probe skipped and reports the unlock URL', async ({ launch }) => {
  const { app, win } = await launch({ fakeHarness: { unlock: 'locked' } });
  await waitReady(win);
  await expect.poll(() => phase(app), { timeout: 30000 }).toBe('up');
  const st = await win.evaluate(() => window.harness.backend.get());
  expect(JSON.stringify(st.problem || {})).toContain('unlock.invalid');
});

test('attach mode: a serve that goes away is reconnected', async ({ app, win, mock }) => {
  await waitReady(win);
  await expect.poll(() => phase(app), { timeout: 15000 }).toBe('up');
  await mainHooks(app, (h) => h.backend.poller.set('t', ['queue']));
  await mock.stop();
  await expect.poll(() => phase(app), { timeout: 20000 }).not.toBe('up');
  await mock.start();
  await expect.poll(() => phase(app), { timeout: 30000 }).toBe('up');
});
