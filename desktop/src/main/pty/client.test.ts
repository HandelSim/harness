import { test } from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { PtyClient, pushReplay, flatReplay } from './client';

class FakeHost extends EventEmitter {
  pid = 4242; sent: { t: string; id?: string }[] = [];
  postMessage(m: { t: string; id?: string }): void {
    this.sent.push(m);
    if (m.t === 'spawn') queueMicrotask(() => this.emit('message', { t: 'spawned', id: m.id, pid: 100 + this.sent.length }));
  }
  kill(): boolean { this.emit('exit', 0); return true; }
}

function mk(exitKeepMs = 30) {
  const host = new FakeHost();
  const events: [string, unknown][] = [];
  const c = new PtyClient({ hostPath: '/x', root: () => '/r', hostConsent: () => false, emit: (ch, p) => events.push([ch, p]),
    fork: () => host as never, exitKeepMs });
  return { c, host, events };
}

test('H3: pty:kill forgets the terminal; an exited one is reaped after the keep window', async () => {
  const { c, host } = mk(30);
  const a = await c.spawn({ kind: 'plain', cwd: '/tmp' }, 80, 24);
  const b = await c.spawn({ kind: 'plain', cwd: '/tmp' }, 80, 24);
  assert.ok(a.ok && b.ok);
  assert.equal(c.list().length, 2);
  assert.equal(c.liveCount(), 2);
  c.kill(a.ok ? a.id : '');
  assert.deepEqual(host.sent.at(-1), { t: 'kill', id: a.ok && a.id });
  assert.deepEqual(c.list().map((t) => t.id), [b.ok && b.id]);
  host.emit('message', { t: 'exit', id: b.ok && b.id, code: 0, signal: 0 });
  assert.equal(c.liveCount(), 0);
  assert.ok(c.attach(b.ok ? b.id : ''), 'still attachable right after exit');
  await new Promise((r) => setTimeout(r, 60));
  assert.deepEqual(c.list(), []);
  assert.equal(c.attach(b.ok ? b.id : ''), null);
});

test('H3: a pty-host crash marks every terminal ended and reaps them', async () => {
  const { c, host, events } = mk(20);
  const a = await c.spawn({ kind: 'plain', cwd: '/tmp' }, 80, 24);
  assert.ok(a.ok);
  host.emit('exit', 1);
  assert.ok(events.some(([ch, p]) => ch === 'pty:exit' && (p as { id: string }).id === (a.ok && a.id)));
  await new Promise((r) => setTimeout(r, 50));
  assert.deepEqual(c.list(), []);
});

test('L4: replay ring keeps the last 256 KiB as chunks and flattens on attach', async () => {
  const t = { chunks: [] as string[], len: 0 };
  for (let i = 0; i < 100; i++) pushReplay(t, String(i % 10).repeat(1000), 10_000);
  assert.ok(t.len >= 10_000 && t.len < 11_000, `len ${t.len}`);
  const flat = flatReplay(t, 10_000);
  assert.equal(flat.length, 10_000);
  assert.equal(flat.slice(-1000), '9'.repeat(1000));
  const { c, host } = mk();
  const a = await c.spawn({ kind: 'plain', cwd: '/tmp' }, 80, 24);
  assert.ok(a.ok);
  for (let i = 0; i < 400; i++) host.emit('message', { t: 'data', id: a.id, data: 'x'.repeat(1024) });
  host.emit('message', { t: 'data', id: a.id, data: 'END' });
  const r = c.attach(a.id)!;
  assert.equal(r.replay.length, 256 * 1024);
  assert.ok(r.replay.endsWith('END'));
});

test('P1: a spawned answer after the wait gave up kills that pty in the host', async () => {
  const { c, host } = mk(30);
  const a = await c.spawn({ kind: 'plain', cwd: '/tmp' }, 80, 24);
  assert.ok(a.ok);
  host.emit('message', { t: 'spawned', id: 'tlate1', pid: 999 });   // no wait for it any more
  assert.deepEqual(host.sent.at(-1), { t: 'kill', id: 'tlate1' });
  assert.equal(c.list().length, 1, 'no record for the late one');
  const n = host.sent.length;
  host.emit('message', { t: 'spawned', id: a.ok ? a.id : '', pid: 1 });  // a duplicate for a live terminal is not killed
  assert.equal(host.sent.length, n);
});
