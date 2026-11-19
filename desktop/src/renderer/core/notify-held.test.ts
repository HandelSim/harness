// N1 (review 2): notifications over the rate limit are held and shown as one summary when the window frees.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { signal } from '@preact/signals-core';
import { startNotify, RateLimit, summary } from './notify';
import type { Queue, TicketStatus } from '../../shared/api';
import type { AppUpdateState, Settings } from '../../shared/ipc';

const q = (running: boolean, ts: [string, TicketStatus][]) => ({ tickets: ts.map(([key, status]) => ({ key, status, title: '' })), runner: { running } }) as unknown as Queue;
const notif = { needsInput: true, ticketDone: true, runEnded: true, update: true };

test('RateLimit.waitMs: time until the oldest slot leaves the window', () => {
  const r = new RateLimit(2, 1000);
  r.allow(0); r.allow(100);
  assert.equal(r.waitMs(200), 801);
  assert.equal(r.waitMs(999), 2);
  assert.equal(r.waitMs(1000), 0);
});

test('N1: the fifth event in a minute (run ended) is shown later as a summary, not dropped', () => {
  let t = 0;
  const timers: { at: number; fn: () => void }[] = [];
  const queue = signal<Queue | undefined>(undefined);
  const update = signal<AppUpdateState | null>(null);
  const settings = signal<Settings | null>({ notifications: notif } as Settings);
  const shown: { title: string; body: string }[] = [];
  const off = startNotify({ queue, update, settings, focused: () => false, show: (n) => { shown.push(n); }, now: () => t,
    later: (fn, ms) => { const e = { at: t + ms, fn }; timers.push(e); return () => { timers.splice(timers.indexOf(e), 1); }; } });
  const keys = ['A', 'B', 'C', 'D'];
  queue.value = q(true, keys.map((k) => [k, 'running']));
  const st: [string, TicketStatus][] = keys.map((k) => [k, 'running']);
  for (let i = 0; i < 4; i++) { t += 1000; st[i] = [keys[i], 'done']; queue.value = q(true, st.map((x) => [...x] as [string, TicketStatus])); }
  assert.equal(shown.length, 4, 'four tickets done, one per poll');
  t += 1000; queue.value = q(false, st.map((x) => [...x] as [string, TicketStatus]));
  assert.equal(shown.length, 4, 'the limit holds');
  assert.equal(timers.length, 1, 'a flush is armed');
  t = timers[0].at; const e = timers.shift()!; e.fn();
  assert.equal(shown.length, 5);
  assert.equal(shown[4].title, 'Queue run ended');
  off();
  assert.equal(timers.length, 0);
});

test('N1: held notes of several kinds become one summary; focus clears them', () => {
  assert.equal(summary([{ kind: 'ticketDone', id: 'a', title: 'Ticket done', body: 'A', route: 'ticket:A' },
    { kind: 'runEnded', id: 'r', title: 'Queue run ended', body: '', route: 'view:queue' }]).title, '2 queue updates');
  let t = 0; let focused = false;
  const timers: (() => void)[] = [];
  const queue = signal<Queue | undefined>(undefined);
  const shown: string[] = [];
  const off = startNotify({ queue, update: signal(null), settings: signal({ notifications: notif } as Settings), focused: () => focused,
    show: (n) => { shown.push(n.title); }, now: () => t, later: (fn) => { timers.push(fn); return () => {}; } });
  queue.value = q(true, [['A', 'running'], ['B', 'running'], ['C', 'running'], ['D', 'running'], ['E', 'running']]);
  for (let i = 0; i < 4; i++) { t = i; queue.value = q(true, ['A', 'B', 'C', 'D'].map((k, j) => [k, j <= i ? 'done' : 'running'] as [string, TicketStatus]).concat([['E', 'running']])); }
  assert.equal(shown.length, 4);
  queue.value = q(true, [['A', 'done'], ['B', 'done'], ['C', 'done'], ['D', 'done'], ['E', 'blocked']]);
  queue.value = q(false, [['A', 'done'], ['B', 'done'], ['C', 'done'], ['D', 'done'], ['E', 'blocked']]);
  t = 70_000; timers.shift()!();
  assert.equal(shown[4], '2 queue updates');
  // held while unfocused, then the owner comes back before the flush: nothing is replayed
  t = 70_002; queue.value = q(true, [['A', 'done'], ['B', 'done'], ['C', 'done'], ['D', 'done'], ['E', 'running']]);
  for (let i = 0; i < 4; i++) { t = 70_003 + i; queue.value = q(true, [['A', 'done'], ['B', 'done'], ['C', 'done'], ['D', 'done'], ['E', i % 2 ? 'running' : 'blocked']]); }
  const n = shown.length;
  focused = true;
  while (timers.length) timers.shift()!();
  assert.equal(shown.length, n, 'nothing shown once focused');
  off();
});
