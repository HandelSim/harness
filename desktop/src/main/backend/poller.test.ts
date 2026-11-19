import { test } from 'node:test';
import assert from 'node:assert/strict';
import { Poller, MAX_TOPICS, type Clock, type PollAnswer } from './poller';

class FakeClock implements Clock {
  t = 0; q: { at: number; fn: () => void; id: number }[] = []; n = 0;
  now() { return this.t; }
  set(fn: () => void, ms: number) { const id = ++this.n; this.q.push({ at: this.t + ms, fn, id }); return id; }
  clear(h: unknown) { this.q = this.q.filter((x) => x.id !== h); }
  async advance(ms: number) {
    const end = this.t + ms;
    for (;;) {
      this.q.sort((a, b) => a.at - b.at);
      const nx = this.q[0];
      if (!nx || nx.at > end) break;
      this.q.shift(); this.t = nx.at; nx.fn();
      for (let i = 0; i < 5; i++) await Promise.resolve();
    }
    this.t = end;
  }
}

/** Answers every requested topic with sig `<topic>-v1` (or `same` when the request already carries it). */
function server() {
  const calls: { topic: string; sig: string }[][] = [];
  const fetch = async (q: { topic: string; sig: string }[]): Promise<PollAnswer> => {
    calls.push(q);
    const topics: Record<string, { sig: string; data?: unknown; same?: true }> = {};
    for (const x of q) topics[x.topic] = x.sig === `${x.topic}-v1` ? { sig: x.sig, same: true } : { sig: `${x.topic}-v1`, data: { big: x.topic } };
    return { ok: true, stale: false, topics };
  };
  return { calls, fetch };
}

test('Poller.set (M1): selecting a ticket sends only the new topic, never re-emits the whole cached set', async () => {
  const clock = new FakeClock();
  const srv = server();
  const emits: string[] = [];
  const p = new Poller({ clock, fetch: srv.fetch, update: (t) => emits.push(t), health: () => {} });
  p.set('win', ['queue', 'status']); p.start(); await clock.advance(0);
  assert.deepEqual(emits.sort(), ['queue', 'status']);
  emits.length = 0;
  for (const k of ['A-1', 'A-2', 'A-3']) {             // J/J/J in the queue list
    p.set('win', ['queue', 'status', `ticket:${k}`]);
    await clock.advance(0);
  }
  assert.deepEqual(emits, ['ticket:A-1', 'ticket:A-2', 'ticket:A-3'], 'queue/status are not re-sent on a selection change');
  // A second owner that newly subscribes to a cached topic gets it once; the first owner's topics are not re-sent.
  emits.length = 0;
  p.set('other', ['queue']);
  assert.deepEqual(emits, ['queue']);
  p.set('other', ['queue']);
  assert.deepEqual(emits, ['queue'], 'unchanged set: no emit');
  p.stop();
});

test('Poller: topic errors are forwarded once and cleared on recovery (M6)', async () => {
  const clock = new FakeClock();
  let answer: PollAnswer = { ok: true, stale: false, topics: { queue: { sig: 'a', data: { n: 1 } } } };
  const ups: [string, string, unknown][] = [];
  const errs: [string, string][] = [];
  const p = new Poller({ clock, fetch: async () => answer, update: (t, s, d) => ups.push([t, s, d]), health: () => {},
    error: (t, e) => errs.push([t, e.code]) });
  p.set('w', ['queue']); p.start(); await clock.advance(0);
  assert.equal(ups.length, 1);
  answer = { ok: true, stale: false, topics: { queue: { error: { code: 'internal', message: 'boom' } } } };
  await clock.advance(3000); await clock.advance(3000);
  assert.deepEqual(errs, [['queue', 'internal']], 'forwarded once, not every tick');
  answer = { ok: true, stale: false, topics: { queue: { sig: 'a', same: true } } };
  await clock.advance(3000);
  assert.deepEqual(ups.at(-1), ['queue', 'a', { n: 1 }], 'recovery with unchanged data re-sends the cached data (same sig)');
  assert.equal(ups.length, 2);
  await clock.advance(3000);
  assert.equal(ups.length, 2, 'no further re-sends');
  p.stop();
});

test('Poller: without an error sink the error goes through update as { error } (the renderer shape)', async () => {
  const clock = new FakeClock();
  const ups: unknown[] = [];
  const p = new Poller({ clock, fetch: async () => ({ ok: true, stale: false, topics: { x: { error: { code: 'e', message: 'm' } } } }),
    update: (_t, _s, d) => ups.push(d), health: () => {} });
  p.set('w', ['x']); p.start(); await clock.advance(0);
  assert.deepEqual(ups, [{ error: { code: 'e', message: 'm' } }]);
  p.stop();
});

test('Poller: more than MAX_TOPICS topics are split over several requests, none dropped', async () => {
  const clock = new FakeClock();
  const srv = server();
  const emits = new Set<string>();
  const p = new Poller({ clock, fetch: srv.fetch, update: (t) => emits.add(t), health: () => {} });
  const many = Array.from({ length: 30 }, (_, i) => `ticket:K-${i}`);
  p.set('w', ['queue', ...many]); p.start(); await clock.advance(0);
  assert.equal(srv.calls.length, 2);
  assert.ok(srv.calls.every((c) => c.length <= MAX_TOPICS));
  assert.equal(emits.size, 31, 'ticket:* topics past the cap still update');
  p.stop();
});

test('Poller cadence (L13): a running queue stays fast while blurred; pollNow skips the wait', async () => {
  const clock = new FakeClock();
  let running = true;
  let n = 0;
  const p = new Poller({ clock, fetch: async () => { n++; return { ok: true, stale: false, topics: { queue: { sig: String(running), data: { runner: { running } } } } }; },
    update: () => {}, health: () => {} });
  p.set('o', ['queue']); p.start(); await clock.advance(0);
  p.setVisibility('blurred');
  assert.equal(p.interval(), 2000);
  running = false; await clock.advance(2000);
  assert.equal(p.interval(), 5000, 'idle + blurred');
  p.setVisibility('hidden');
  assert.equal(p.interval(), 15000);
  const before = n;
  p.pollNow(); await clock.advance(0);
  assert.equal(n, before + 1, 'pollNow polls without waiting out the 15 s');
  p.stop();
});
