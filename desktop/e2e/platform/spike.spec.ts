// [U] items settled in M0 (docs/progress/L2.md): a module worker loads from app://, WebGL2 under xvfb, CSP holds.
import { test, expect, waitReady } from '../fixtures';

test('a module worker loads from app:// and WebGL2 is available under xvfb', async ({ win }) => {
  await waitReady(win);
  const r = await win.evaluate(async () => {
    const worker = await new Promise<string>((resolve) => {
      try {
        const w = new Worker('assets/bg-worker.js', { type: 'module' });
        const t = setTimeout(() => { w.terminate(); resolve('loaded'); }, 1500);
        w.onerror = (e) => { clearTimeout(t); resolve(`error: ${(e as ErrorEvent).message || 'load failed'}`); };
      } catch (e) { resolve(`throw: ${String(e)}`); }
    });
    const gl = !!new OffscreenCanvas(4, 4).getContext('webgl2');
    const fetchOut = await fetch('https://example.invalid/').then(() => 'allowed', () => 'blocked');
    return { worker, gl, fetchOut, crossOriginIsolated: self.crossOriginIsolated };
  });
  test.info().annotations.push({ type: 'spike', description: JSON.stringify(r) });
  console.log('spike', JSON.stringify(r));
  expect(r.worker).toBe('loaded');
  expect(r.fetchOut).toBe('blocked');
});
