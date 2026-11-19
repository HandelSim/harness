// PTY host (utilityProcess + node-pty): spawn, echo, resize, flow control under a flood, kill, replay on attach.
import { test, expect, mainHooks, waitReady } from '../fixtures';

test('a plain shell echoes, survives a 4 MB flood with flow control, replays on attach and exits on kill', async ({ app, win }) => {
  test.setTimeout(90000);
  await waitReady(win);
  const r = await win.evaluate(async () => {
    const h = window.harness;
    let out = ''; let total = 0; let exited: unknown = null; let maxUnacked = 0; let unacked = 0;
    const s = await h.pty.spawn({ kind: 'plain', cwd: '/tmp' }, 100, 30);
    if (!s.ok) return { err: s.error };
    const id = s.id;
    const off1 = h.on('pty:data', (p) => {
      if (p.id !== id) return;
      total += p.data.length; unacked += p.data.length; maxUnacked = Math.max(maxUnacked, unacked);
      if (out.length < 200000) out += p.data;
      // a slow consumer: ack in steps, late
      setTimeout(() => { h.pty.ack(id, p.data.length); unacked -= p.data.length; }, 5);
    });
    const off2 = h.on('pty:exit', (p) => { if (p.id === id) exited = p; });
    const wait = async (f: () => boolean, ms: number) => { const t0 = Date.now(); while (!f() && Date.now() - t0 < ms) await new Promise((r) => setTimeout(r, 50)); return f(); };
    h.pty.write(id, 'echo hello-$((40+2))\r');
    const echoed = await wait(() => out.includes('hello-42'), 10000);
    h.pty.resize(id, 120, 40);
    h.pty.write(id, "stty size; head -c 4000000 /dev/zero | tr '\\0' 'x'; echo; echo flood-done\r");
    const flooded = await wait(() => out.includes('flood-done') || total > 4000000 && out.length >= 200000, 60000);
    await wait(() => total >= 4000000, 30000);
    const list = await h.pty.list();
    const att = await h.pty.attach(id);
    h.pty.kill(id);
    const gone = await wait(() => exited !== null, 10000);
    off1(); off2();
    return { echoed, flooded, total, sized: out.includes('40 120'), maxUnacked, listed: list.some((x) => x.id === id),
      replayLen: att?.replay.length ?? -1, gone };
  });
  console.log('pty', JSON.stringify(r));
  expect(r).not.toHaveProperty('err');
  expect(r.echoed).toBe(true);
  expect(r.sized).toBe(true);
  expect(r.total).toBeGreaterThanOrEqual(4000000);
  expect(r.listed).toBe(true);
  expect(r.replayLen).toBeGreaterThan(0);
  expect(r.replayLen).toBeLessThanOrEqual(256 * 1024 + 4096);
  expect(r.gone).toBe(true);
  // the host pauses above 1 MiB unacked, so the renderer never holds much more than that
  expect(r.maxUnacked).toBeLessThan(3 * 1024 * 1024);
  const stats = await mainHooks(app, (h) => h.pty.stats());
  console.log('pty stats', JSON.stringify(stats));
});

test('the renderer cannot spawn a pty with a bad spec', async ({ win }) => {
  await waitReady(win);
  const r = await win.evaluate(async () => {
    const h = window.harness;
    const a = await h.pty.spawn({ kind: 'plain', cwd: 'relative/dir' } as never, 80, 24);
    const b = await h.pty.spawn({ kind: 'evil', cwd: '/tmp' } as never, 80, 24);
    const c = await h.pty.spawn({ kind: 'agent', cwd: '/tmp', ticket: 'x; rm -rf /' } as never, 80, 24);
    return [a.ok, b.ok, c.ok];
  });
  expect(r).toEqual([false, false, false]);
});
