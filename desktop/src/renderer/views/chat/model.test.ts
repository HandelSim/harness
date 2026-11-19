import { test } from 'node:test';
import assert from 'node:assert/strict';
import type { SessionRow } from '../../../shared/api';
import { chatRows, pickSession, rowMeta, step } from './model';

const R = (id: string, o: Partial<SessionRow> = {}): SessionRow => ({ id, parent: null, source: 'container', directory: '/w/proj', title: id, agent: '', model: 'm',
  created: 1, updated: 1, tokens_in: 0, tokens_out: 0, cost: 0, kids: 0, working: false, live: false, ticket: null, kind: 'session', label: '', web: null, ...o } as SessionRow);

test('chatRows puts live runs first, filters and scopes', () => {
  const rows = [R('a', { updated: 30 }), R('b', { live: true, updated: 10 }), R('c', { working: true, updated: 5 }), R('d', { live: true, working: true, updated: 1, title: 'Parser fix' })];
  assert.deepEqual(chatRows(rows, '', 'all').map((r) => r.id), ['d', 'b', 'c', 'a']);
  assert.deepEqual(chatRows(rows, '', 'live').map((r) => r.id), ['d', 'b']);
  assert.deepEqual(chatRows(rows, 'parser', 'all').map((r) => r.id), ['d']);
  assert.equal(chatRows(rows, '', 'all', 2).length, 2);
  assert.deepEqual(chatRows(undefined, '', 'all'), []);
});

test('pickSession keeps the current one while listed, else the first live, else the first', () => {
  const list = [R('a'), R('b', { live: true })];
  assert.equal(pickSession(list, 'a'), 'a');
  assert.equal(pickSession(list, 'gone'), 'b');
  assert.equal(pickSession([R('x')], ''), 'x');
  assert.equal(pickSession([], 'x'), '');
});

test('rowMeta and step', () => {
  assert.equal(rowMeta(R('a', { ticket: 'T-1', directory: '/w/proj/' })), 'T-1 · m · proj');
  assert.equal(rowMeta(R('a', { kind: 'findings', model: '' })), 'findings · proj');
  const list = [R('a'), R('b'), R('c')];
  assert.equal(step(list, 'a', 1), 'b');
  assert.equal(step(list, 'c', 1), 'c');
  assert.equal(step(list, 'a', -1), 'a');
  assert.equal(step(list, 'zz', -1), 'c');
  assert.equal(step([], 'a', 1), '');
});
