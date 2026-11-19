// Key vault against the fake harness: add + activate writes .env through `harness config set`; secrets never reach the renderer.
import { test, expect, waitReady, fakeState } from '../fixtures';

test('add and activate an upstream key; the renderer only ever sees metadata', async ({ launch }) => {
  const { win, root } = await launch({ fakeHarness: {} });
  await waitReady(win);
  const secret = 'sk-test-SECRET-value-98765';
  const st0 = await win.evaluate(() => window.harness.keys.list());
  console.log('vault storage', st0.backend, 'secure', st0.secure);
  expect(st0.env.PROXY_API_KEY?.set).toBe(true);
  const r = await win.evaluate((s) => window.harness.keys.add({ kind: 'PROXY_API_KEY', label: 'team key', secret: s, activate: true }), secret);
  expect(r.ok, JSON.stringify(r)).toBe(true);
  expect(JSON.stringify(r)).not.toContain(secret);
  const k = r.state.keys.find((x) => x.label === 'team key')!;
  expect(k.active).toBe(true);
  expect(k.last4).toBe('8765');
  expect(k.locksAt).toBeGreaterThan(Date.now());
  const fs = fakeState(root!);
  expect(fs.config.PROXY_API_KEY).toBe(secret);
  expect(fs.calls.map((c) => c.join(' '))).toContain(`config set PROXY_API_KEY <${secret.length} chars>`);
  const p = await win.evaluate((id) => window.harness.keys.probe({ id }), k.id);
  expect(p.state).toBe('ok');
  // an active key cannot be removed
  expect((await win.evaluate((id) => window.harness.keys.remove(id), k.id)).ok).toBe(false);
});

test('a rejected key is reported, and an inactive key is refused without a keyring', async ({ launch }) => {
  const { win } = await launch({ fakeHarness: { unlock: 'rejected' } });
  await waitReady(win);
  const st = await win.evaluate(() => window.harness.keys.list());
  const r = await win.evaluate(() => window.harness.keys.add({ kind: 'PROXY_API_KEY', label: 'bad', secret: 'sk-bad-0000', activate: true }));
  expect(r.ok).toBe(false);
  expect(JSON.stringify(r)).toContain('rejects');
  if (!st.secure) {
    const i = await win.evaluate(() => window.harness.keys.add({ kind: 'OAI_API_KEY', label: 'spare', secret: 'sk-spare-1' }));
    expect(i.ok).toBe(false);
    expect(!i.ok && i.error.code).toBe('insecure');
  }
});
