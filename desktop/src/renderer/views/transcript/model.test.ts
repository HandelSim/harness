import { test } from 'node:test';
import assert from 'node:assert/strict';
import type { Message, SessionRow } from '../../../shared/api';
import { mergePage, nextFrom, partView, sortSessions, tailStart, TAIL } from './model';

const M = (index: number, text = `m${index}`): Message => ({ id: `id${index}`, index, created: index, role: 'assistant', info: {}, parts: [{ id: `p${index}`, type: 'text', text }] });

test('mergePage appends, replaces the growing last message and trims to count', () => {
  let l = mergePage([], { from: 0, count: 2, messages: [M(0), M(1, 'part')], working: true, live: true });
  assert.equal(l.length, 2);
  assert.equal(nextFrom(l), 1);
  l = mergePage(l, { from: 1, count: 3, messages: [M(1, 'partial done'), M(2)], working: true, live: true });
  assert.deepEqual(l.map((m) => m.index), [0, 1, 2]);
  assert.equal((l[1].parts[0] as unknown as { text: string }).text, 'partial done');
  l = mergePage(l, { from: 0, count: 1, messages: [M(0)], working: false, live: false });
  assert.equal(l.length, 1, 'a shorter session (reset) wins');
  assert.equal(nextFrom([]), 0);
});

test('partView maps parts and hides bookkeeping', () => {
  assert.deepEqual(partView({ id: 'a', type: 'text', text: 'hi' }), { kind: 'text', text: 'hi' });
  assert.equal(partView({ id: 'a', type: 'text', text: '  ' }), null);
  assert.equal(partView({ id: 'a', type: 'step-start' }), null);
  const t = partView({ id: 'a', type: 'tool', tool: 'bash', state: { status: 'completed', input: { command: 'ls' }, output: 'x' } });
  assert.equal(t?.kind, 'tool');
  assert.match((t as { input: string }).input, /"command": "ls"/);
  assert.deepEqual(partView({ id: 'a', type: 'patch' }), { kind: 'other', label: 'patch' });
});

test('sortSessions puts working first then most recent, and filters', () => {
  const R = (id: string, updated: number, working = false, title = id) => ({ id, updated, working, title, directory: '/w', agent: 'a', model: 'm' } as SessionRow);
  const rows = [R('a', 1), R('b', 5), R('c', 2, true, 'Fix parser')];
  assert.deepEqual(sortSessions(rows).map((r) => r.id), ['c', 'b', 'a']);
  assert.deepEqual(sortSessions(rows, 'parser').map((r) => r.id), ['c']);
});

test('mergePage (M7): an unchanged re-read returns the same array; a growing last message keeps the others', () => {
  const page = (from: number, ms: Message[]) => ({ from, count: 5000, messages: ms, working: true, live: true });
  const all = Array.from({ length: 5000 }, (_, i) => M(i, `m${i}`));
  const l = mergePage([], page(0, all));
  assert.equal(l.length, 5000);
  const ipc = (m: Message) => JSON.parse(JSON.stringify(m)) as Message;   // every poll hands back new objects
  assert.equal(mergePage(l, page(4999, [ipc(l[4999])])), l, 'same content: same array, no render');
  const grown = mergePage(l, page(4998, [ipc(l[4998]), M(4999, 'm4999 more')]));
  assert.notEqual(grown, l);
  assert.equal(grown[4998], l[4998], 'unchanged message keeps identity (memoised view skips it)');
  assert.notEqual(grown[4999], l[4999]);
  assert.equal(grown[0], l[0]);
});

test('tailStart: only the newest TAIL of a 5,000-message transcript is rendered until "Show earlier"', () => {
  assert.equal(5000 - tailStart(5000, TAIL), TAIL);
  assert.equal(tailStart(5000, TAIL * 2), 5000 - TAIL * 2);
  assert.equal(tailStart(50, TAIL), 0);
});
