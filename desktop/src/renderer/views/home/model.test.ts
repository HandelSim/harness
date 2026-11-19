import { test } from 'node:test';
import assert from 'node:assert/strict';
import type { Queue, TicketSummary } from '../../../shared/api';
import { activityRows, filterPages, greeting, pubPath, queueSummary, usageView } from './model';

test('usageView reads read_ledger tuples and scales model bars to the biggest', () => {
  assert.equal(usageView(null), null);
  assert.equal(usageView('x'), null);
  const u = usageView({ day: { replies: 3, prompt: 100, completion: 50, reasoning: 5, cached: 7, total: 150 }, hour: { replies: 1, total: 20 },
    models: [['big', [2, 80, 40]], ['small', [1, 20, 10]], 'junk', [3, 'bad']], remaining: [['requests', '10'], ['tokens', '900']] })!;
  assert.deepEqual(u.tiles.map((t) => t.value), [3, 100, 50, 150]);
  assert.deepEqual(u.models.map((m) => [m.name, m.total, m.pct]), [['big', 120, 100], ['small', 30, 25]]);
  assert.equal(u.remaining, 'requests 10, tokens 900');
  assert.deepEqual(u.hour, { replies: 1, total: 20 });
  const empty = usageView({})!;
  assert.deepEqual(empty.tiles.map((t) => t.value), [0, 0, 0, 0]);
  assert.deepEqual(empty.models, []);
});

const T = (key: string, status: TicketSummary['status']): TicketSummary => ({ key, title: key, status } as TicketSummary);

test('queueSummary counts by status, lists tickets that need the owner and says what the runner does', () => {
  assert.equal(queueSummary(null), null);
  const q = { tickets: [T('A-1', 'running'), T('A-2', 'needs-answers'), T('A-3', 'plan-review'), T('A-4', 'queued'), T('A-5', 'blocked'), T('A-6', 'done'), T('A-7', 'stopped')],
    runner: { running: true, ticket: 'A-1', stage: 'implement', step: '' }, archived: [] } as unknown as Queue;
  const s = queueSummary(q)!;
  assert.deepEqual([s.total, s.running, s.waiting, s.queued, s.stuck, s.done], [7, 1, 2, 1, 2, 1]);
  assert.equal(s.runner, 'running A-1 implement');
  assert.equal(s.runnerOn, true);
  assert.deepEqual(s.attention.map((a) => a.key), ['A-2', 'A-3']);
  assert.equal(queueSummary({ tickets: [], runner: { halting: true } } as unknown as Queue)!.runner, 'halting');
  assert.equal(queueSummary({ tickets: [], runner: {} } as unknown as Queue)!.runner, 'idle');
});

test('activityRows drops rows without a subject and caps', () => {
  const rows = activityRows([{ sha: 'a1', author: 'me', when: '1 h ago', subject: 'ingest' }, { sha: 'b' }, null, 'x'], 5);
  assert.deepEqual(rows, [{ sha: 'a1', author: 'me', when: '1 h ago', subject: 'ingest' }]);
  assert.deepEqual(activityRows(undefined), []);
  assert.equal(activityRows(Array.from({ length: 50 }, (_, i) => ({ subject: `s${i}` })), 30).length, 30);
});

test('filterPages matches title or path and caps', () => {
  const pages = [{ rel: 'a/index.html', title: 'Index' }, { rel: 'b/report.html', title: 'Weekly' }];
  assert.deepEqual(filterPages(pages, 'report').shown.map((p) => p.title), ['Weekly']);
  assert.deepEqual(filterPages(pages, 'INDEX').shown.map((p) => p.rel), ['a/index.html']);
  const r = filterPages(pages, '', 1);
  assert.equal(r.shown.length, 1); assert.equal(r.more, 1); assert.equal(r.total, 2);
  assert.equal(filterPages(undefined, 'x').total, 0);
});

test('greeting by hour', () => {
  assert.equal(greeting(3), 'Working late');
  assert.equal(greeting(9), 'Good morning');
  assert.equal(greeting(13), 'Good afternoon');
  assert.equal(greeting(20), 'Good evening');
});

test('pubPath encodes each segment of rel (never the raw server url)', () => {
  assert.equal(pubPath('a/b c.html'), '/pub/a/b%20c.html');
  assert.equal(pubPath('x/50% #1?.html'), '/pub/x/50%25%20%231%3F.html');
});
