import { test } from 'node:test';
import assert from 'node:assert/strict';
import { signal } from '@preact/signals-core';
import { queueEvents, snapOf, collapse, RateLimit, startNotify, parseRoute } from './notify';
import type { Queue, TicketStatus } from '../../shared/api';
import type { AppUpdateState, Settings } from '../../shared/ipc';

const q = (running: boolean, ts: [string, TicketStatus][]): Queue => ({
  runner: { running } as Queue['runner'], archived: [], start: { ok: true } as never,
  tickets: ts.map(([key, status]) => ({ key, status, title: `t ${key}`, reason: '' } as Queue['tickets'][number])) });
const notif = { needsInput: true, ticketDone: true, runEnded: true, key: true, update: true };

test('queueEvents: baseline is silent; asks, blocked, done and run end are reported', () => {
  assert.deepEqual(queueEvents(null, q(true, [['A', 'needs-answers']])), []);
  const prev = snapOf(q(true, [['A', 'running'], ['B', 'running'], ['C', 'running'], ['D', 'done']]));
  const ev = queueEvents(prev, q(false, [['A', 'needs-answers'], ['B', 'blocked'], ['C', 'done'], ['D', 'done'], ['E', 'done']]));
  assert.deepEqual(ev.map((e) => [e.kind, e.route]), [['needsInput', 'ticket:A'], ['needsInput', 'ticket:B'], ['ticketDone', 'ticket:C'], ['runEnded', 'view:queue']]);
});

test('collapse: several of one kind become one note', () => {
  const prev = snapOf(q(true, [['A', 'running'], ['B', 'running']]));
  const c = collapse(queueEvents(prev, q(true, [['A', 'plan-review'], ['B', 'needs-answers']])));
  assert.equal(c.length, 1);
  assert.equal(c[0].title, '2 tickets need you');
  assert.equal(c[0].route, 'view:queue');
});

test('RateLimit: at most N per window', () => {
  const r = new RateLimit(2, 1000);
  assert.deepEqual([r.allow(0), r.allow(1), r.allow(2), r.allow(1000), r.allow(1000)], [true, true, false, true, false]);
});

test('startNotify: only while unfocused, honours per-kind settings, update ready once', () => {
  const queue = signal<Queue | undefined>(undefined);
  const update = signal<AppUpdateState | null>(null);
  const settings = signal<Settings | null>({ notifications: { ...notif, ticketDone: false } } as Settings);
  let focused = true;
  const shown: { title: string; route: string }[] = [];
  const off = startNotify({ queue, update, settings, focused: () => focused, show: (n) => { shown.push(n); } });
  queue.value = q(true, [['A', 'running'], ['B', 'running']]);
  queue.value = q(true, [['A', 'needs-answers'], ['B', 'running']]);
  assert.equal(shown.length, 0, 'focused: silent');
  focused = false;
  queue.value = q(true, [['A', 'needs-answers'], ['B', 'done']]);
  assert.equal(shown.length, 0, 'ticketDone off');
  queue.value = q(false, [['A', 'needs-answers'], ['B', 'done']]);
  assert.deepEqual(shown.map((s) => s.route), ['view:queue']);
  update.value = { phase: 'downloading' } as AppUpdateState;
  update.value = { phase: 'ready', next: '1.2.3' } as AppUpdateState;
  update.value = { phase: 'ready', next: '1.2.3' } as AppUpdateState;
  assert.deepEqual(shown.map((s) => s.route), ['view:queue', 'view:updates']);
  off();
});

test('parseRoute', () => {
  assert.deepEqual(parseRoute('ticket:ABC-1'), { kind: 'ticket', key: 'ABC-1' });
  assert.deepEqual(parseRoute('view:queue'), { kind: 'view', view: 'queue' });
  assert.equal(parseRoute('javascript:x'), null);
});
