import { test } from 'node:test';
import assert from 'node:assert/strict';
import type { JiraRow } from '../../../shared/api';
import { addableKeys, filterRows, jiraQuery, statusTone } from './model';

const row = (key: string, o: Partial<JiraRow> = {}): JiraRow =>
  ({ key, summary: `Summary ${key}`, status: 'To Do', assignee: 'Ann', type: 'Bug', priority: 'High', queued: null, ...o });

test('jiraQuery: list and mine=1 only when mine', () => {
  assert.deepEqual(jiraQuery('sprint', false), { list: 'sprint' });
  assert.deepEqual(jiraQuery('backlog', true), { list: 'backlog', mine: '1' });
});

test('filterRows matches key, summary, status, assignee, type; empty query keeps all', () => {
  const rows = [row('A-1', { summary: 'Fix login' }), row('B-2', { assignee: 'Bob', status: 'In Progress' })];
  assert.equal(filterRows(rows, '').length, 2);
  assert.deepEqual(filterRows(rows, 'login').map((r) => r.key), ['A-1']);
  assert.deepEqual(filterRows(rows, 'bob').map((r) => r.key), ['B-2']);
  assert.deepEqual(filterRows(rows, 'b-2').map((r) => r.key), ['B-2']);
  assert.deepEqual(filterRows(rows, 'progress').map((r) => r.key), ['B-2']);
  assert.deepEqual(filterRows(rows, 'nothing'), []);
});

test('statusTone: category first, else status words', () => {
  assert.equal(statusTone({ status: 'Whatever', cat: 'done' }), 'ok');
  assert.equal(statusTone({ status: 'Whatever', cat: 'indeterminate' }), 'info');
  assert.equal(statusTone({ status: 'Done', cat: 'new' }), 'default');
  assert.equal(statusTone({ status: 'Closed' }), 'ok');
  assert.equal(statusTone({ status: 'In Review' }), 'info');
  assert.equal(statusTone({ status: 'To Do' }), 'default');
});

test('addableKeys: picked, not queued, in row order, once', () => {
  const rows = [row('A-1'), row('A-2', { queued: 'done' }), row('A-3'), row('A-1')];
  assert.deepEqual(addableKeys(rows, new Set(['A-3', 'A-1', 'A-2'])), ['A-1', 'A-3']);
  assert.deepEqual(addableKeys(rows, new Set()), []);
});
