import { test } from 'node:test';
import assert from 'node:assert/strict';
import { debugId, filterDebug, pickSelection, reportPath, verdictTone, type DebugRow } from './model';

const r = (kind: string, name: string, o: Partial<DebugRow> = {}): DebugRow =>
  ({ kind, name, mtime: 1, key: '', verdict: '', change: false, title: '', session: '', ...o });

test('debugId and reportPath encode the kind and name', () => {
  assert.equal(debugId(r('runs', 'A-1-x')), 'runs/A-1-x');
  assert.equal(reportPath(r('reports', 'a b#c')), '/debug/reports/a%20b%23c');
});

test('filterDebug by kind and text (name, key, title, verdict), keeps server order', () => {
  const rows = [r('runs', 'r1', { key: 'ABC-1', verdict: 'running' }), r('reports', 'p1', { title: 'Login breaks' }), r('runs', 'r2', { verdict: 'bug' })];
  assert.deepEqual(filterDebug(rows, 'all', '').map(debugId), ['runs/r1', 'reports/p1', 'runs/r2']);
  assert.deepEqual(filterDebug(rows, 'runs', '').map(debugId), ['runs/r1', 'runs/r2']);
  assert.deepEqual(filterDebug(rows, 'reports', '').map(debugId), ['reports/p1']);
  assert.deepEqual(filterDebug(rows, 'all', 'abc-1').map(debugId), ['runs/r1']);
  assert.deepEqual(filterDebug(rows, 'all', 'LOGIN').map(debugId), ['reports/p1']);
  assert.deepEqual(filterDebug(rows, 'reports', 'bug'), []);
});

test('verdictTone', () => {
  assert.equal(verdictTone(''), 'default');
  assert.equal(verdictTone('running'), 'info');
  assert.equal(verdictTone('not a bug'), 'ok');
  assert.equal(verdictTone('bug'), 'err');
  assert.equal(verdictTone('inconclusive'), 'warn');
  assert.equal(verdictTone('something else'), 'default');
});

test('pickSelection keeps a selection still listed, else the first, else empty', () => {
  const rows = [r('runs', 'a'), r('reports', 'b')];
  assert.equal(pickSelection(rows, 'reports/b'), 'reports/b');
  assert.equal(pickSelection(rows, 'runs/gone'), 'runs/a');
  assert.equal(pickSelection(rows, undefined), 'runs/a');
  assert.equal(pickSelection([], 'runs/a'), '');
});
