import { test } from 'node:test';
import assert from 'node:assert/strict';
import type { Runner, TicketStatus, TicketSummary } from '../../../shared/api';
import { rowPill, liveKeys, runnerCard, counts, filterTickets, moveLocal, reconcile, neighbourBefore, orderTickets, keepSelection, slotStage } from './model';

const R = (p: Partial<Runner> = {}): Runner => ({ running: false, starting: false, halting: false, exited: null, ticket: '', stage: '', step: '', model: '',
  started: null, since: null, host: false, single: false, watch: false, parallel: 1, on: [], pid: 0, beat: null, ...p });
const T = (key: string, status: TicketStatus, p: Partial<TicketSummary> = {}): TicketSummary => ({ key, title: `title ${key}`, status, stage: '', step: '', reason: '',
  created: null, updated: null, finished: null, attempt: 1, project: 'proj', branch: '', stopping: false, debug: false, session: null, actions: {} as never, ...p });

const STATUSES: TicketStatus[] = ['queued', 'running', 'interrupted', 'needs-answers', 'plan-review', 'blocked', 'stopped', 'done'];
const RUNNERS: Record<string, Runner | null> = {
  none: null, idle: R(), starting: R({ starting: true }), runningThis: R({ running: true, ticket: 'A-1', stage: 'work' }),
  runningOther: R({ running: true, ticket: 'B-2' }), haltingThis: R({ running: true, halting: true, ticket: 'A-1' }),
  slotThis: R({ running: true, ticket: 'B-2', parallel: 2, on: [{ ticket: 'A-1', stage: 'verify' }] }), exited: R({ exited: { code: 1 } }),
};

test('status x runner matrix: every cell has a non-empty label and a valid tone', () => {
  for (const s of STATUSES) for (const [name, r] of Object.entries(RUNNERS)) {
    const p = rowPill(T('A-1', s), r);
    assert.ok(p.label.length > 0, `${s}/${name}`);
    assert.ok(['ok', 'warn', 'err', 'info', 'accent', 'default'].includes(p.tone), `${s}/${name}`);
  }
});

test('running ticket: live, halting, starting and orphaned', () => {
  assert.deepEqual([rowPill(T('A-1', 'running'), RUNNERS.runningThis).tone, rowPill(T('A-1', 'running'), RUNNERS.runningThis).dot], ['accent', 'live']);
  assert.equal(rowPill(T('A-1', 'running'), RUNNERS.haltingThis).label, 'halting');
  assert.equal(rowPill(T('A-1', 'running'), RUNNERS.starting).tone, 'info');
  const orphan = rowPill(T('A-1', 'running'), RUNNERS.idle);
  assert.equal(orphan.tone, 'warn');
  assert.equal(orphan.live, false);
  assert.match(orphan.hint, /no runner/);
  assert.equal(rowPill(T('A-1', 'running'), RUNNERS.runningOther).live, false);
});

test('parallel slots count as live', () => {
  assert.equal(rowPill(T('A-1', 'running'), RUNNERS.slotThis).live, true);
  assert.deepEqual([...liveKeys(RUNNERS.slotThis)].sort(), ['A-1', 'B-2']);
  assert.deepEqual([...liveKeys(R({ running: true, on: ['X-1', '', 3] as unknown[] }))], ['X-1']);
  assert.equal(slotStage(RUNNERS.slotThis, 'A-1'), 'verify');
  assert.equal(slotStage(RUNNERS.runningThis, 'A-1'), 'work');
  assert.equal(liveKeys(R({ ticket: 'A-1' })).size, 0, 'a stopped runner has no live keys');
});

test('stopping wins over every status', () => {
  for (const s of STATUSES) assert.equal(rowPill(T('A-1', s, { stopping: true }), RUNNERS.runningThis).label, 'stopping…');
});

test('attention statuses and queued hints', () => {
  assert.equal(rowPill(T('A-1', 'needs-answers'), null).attention, true);
  assert.equal(rowPill(T('A-1', 'plan-review'), null).attention, true);
  assert.equal(rowPill(T('A-1', 'blocked'), null).attention, false);
  assert.match(rowPill(T('A-1', 'queued'), RUNNERS.idle).hint, /start/);
  assert.equal(rowPill(T('A-1', 'queued'), RUNNERS.runningOther).hint, '');
  assert.equal(rowPill(T('A-1', 'interrupted'), null).tone, 'warn');
  assert.equal(rowPill(T('A-1', 'done'), null).tone, 'ok');
  assert.equal(rowPill(T('A-1', 'bogus' as TicketStatus), null).label, 'bogus');
});

test('runner card phases', () => {
  assert.equal(runnerCard(null).phase, 'idle');
  assert.equal(runnerCard(RUNNERS.idle).phase, 'idle');
  assert.equal(runnerCard(RUNNERS.starting).phase, 'starting');
  assert.equal(runnerCard(RUNNERS.runningThis).phase, 'running');
  assert.equal(runnerCard(RUNNERS.runningThis).title, 'Running A-1');
  assert.equal(runnerCard(RUNNERS.haltingThis).phase, 'halting');
  assert.equal(runnerCard(RUNNERS.exited).phase, 'exited');
  assert.equal(runnerCard(RUNNERS.exited).detail, 'code 1');
  assert.equal(runnerCard(R({ host: true, running: true })).mode, 'host');
  assert.equal(runnerCard(R({ single: true, host: true, running: true })).mode, 'single message');
  assert.equal(runnerCard(RUNNERS.slotThis).busy, 2);
  assert.equal(runnerCard(RUNNERS.slotThis).slots, 2);
});

test('counts and filter', () => {
  const list = [T('A-1', 'queued'), T('A-2', 'needs-answers'), T('A-3', 'plan-review'), T('B-1', 'running', { title: 'Fix login' }), T('B-2', 'done'), T('B-3', 'blocked'), T('C-1', 'interrupted')];
  assert.deepEqual(counts(list), { total: 7, attention: 2, running: 1, queued: 2, blocked: 1, done: 1 });
  assert.deepEqual(filterTickets(list, 'fix LOGIN').map((t) => t.key), ['B-1']);
  assert.deepEqual(filterTickets(list, 'a-').map((t) => t.key), ['A-1', 'A-2', 'A-3']);
  assert.equal(filterTickets(list, '  ').length, 7);
});

test('optimistic reorder reconciles with the server', () => {
  const server = ['A', 'B', 'C', 'D'];
  assert.deepEqual(moveLocal(server, 'D', 'B'), ['A', 'D', 'B', 'C']);
  assert.deepEqual(moveLocal(server, 'A', null), ['B', 'C', 'D', 'A']);
  assert.deepEqual(moveLocal(server, 'X', 'A'), server);
  assert.deepEqual(moveLocal(server, 'A', 'X'), server);
  const pending = { key: 'D', order: ['A', 'D', 'B', 'C'], at: 1000 };
  assert.deepEqual(reconcile(server, pending, 2000).order, pending.order, 'local order shown while in flight');
  assert.equal(reconcile(['A', 'D', 'B', 'C'], pending, 2000).pending, null, 'server caught up');
  assert.deepEqual(reconcile(server, pending, 7000).order, server, 'server wins after ttl');
  assert.deepEqual(reconcile(['A', 'B', 'C'], pending, 2000).order, ['A', 'B', 'C'], 'set changed -> server wins');
  assert.deepEqual(reconcile(server, null, 0), { order: server, pending: null });
});

test('neighbourBefore for Alt+Up/Down', () => {
  const o = ['A', 'B', 'C'];
  assert.equal(neighbourBefore(o, 'B', 'up'), 'A');
  assert.equal(neighbourBefore(o, 'A', 'up'), undefined);
  assert.equal(neighbourBefore(o, 'A', 'down'), 'C');
  assert.equal(neighbourBefore(o, 'B', 'down'), null);
  assert.equal(neighbourBefore(o, 'C', 'down'), undefined);
  assert.deepEqual(moveLocal(o, 'A', neighbourBefore(o, 'A', 'down') as string), ['B', 'A', 'C']);
  assert.deepEqual(moveLocal(o, 'B', neighbourBefore(o, 'B', 'down') as null), ['A', 'C', 'B']);
});

test('orderTickets and keepSelection', () => {
  const list = [T('A', 'queued'), T('B', 'queued'), T('C', 'queued')];
  assert.deepEqual(orderTickets(list, ['C', 'A']).map((t) => t.key), ['C', 'A', 'B']);
  assert.equal(keepSelection('B', 1, list), 'B');
  assert.equal(keepSelection('Z', 5, list), 'C');
  assert.equal(keepSelection(null, 0, list), null);
  assert.equal(keepSelection('A', 0, []), null);
});
