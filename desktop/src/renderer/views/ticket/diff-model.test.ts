import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  commentWhere, DIFF_CHAR_CAP, fixItems, gapAbove, insertLines, LINES_CAP, parseDiff, putComment, reviewError, rowAnchor,
  totals, viewTakesComments, type ReviewComment,
} from './diff-model';
import { FIX_MAX_CHARS, FIX_MAX_COMMENTS } from './model';

const DIFF = [
  'diff --git a/src/a.ts b/src/a.ts',
  'index 111..222 100644',
  '--- a/src/a.ts',
  '+++ b/src/a.ts',
  '@@ -10,3 +10,4 @@ function f() {',
  ' keep',
  '-old line',
  '+new line',
  '+added',
  ' tail',
  '@@ -40,2 +41,2 @@',
  ' x',
  '-y',
  '+z',
  'diff --git a/new.txt b/new.txt',
  'new file mode 100644',
  '--- /dev/null',
  '+++ b/new.txt',
  '@@ -0,0 +1 @@',
  '+hello',
  'diff --git a/gone.txt b/gone.txt',
  'deleted file mode 100644',
  '--- a/gone.txt',
  '+++ /dev/null',
  '@@ -1 +0,0 @@',
  '-bye',
  '',
].join('\n');

test('parseDiff: files, statuses, rows with line numbers, totals', () => {
  const { files, cut } = parseDiff(DIFF);
  assert.equal(cut, false);
  assert.deepEqual(files.map((f) => [f.path, f.status]), [['src/a.ts', 'modified'], ['new.txt', 'added'], ['gone.txt', 'deleted']]);
  const a = files[0];
  assert.equal(a.rows[0].kind, 'hunk');
  assert.deepEqual(a.rows.slice(1, 6).map((r) => [r.kind, r.old, r.new, r.text]), [
    ['ctx', 10, 10, 'keep'], ['del', 11, undefined, 'old line'], ['add', undefined, 11, 'new line'],
    ['add', undefined, 12, 'added'], ['ctx', 12, 13, 'tail']]);
  assert.deepEqual(totals(files), { files: 3, added: 4, deleted: 3 });
});

test('parseDiff: empty and garbage never throw', () => {
  assert.deepEqual(parseDiff('').files, []);
  assert.doesNotThrow(() => parseDiff('not a diff at all\n@@ nonsense'));
});

test('parseDiff: a diff over the cap is cut at a file boundary', () => {
  const one = DIFF.split('diff --git a/new.txt')[0];
  const big = one + 'diff --git a/big b/big\n--- a/big\n+++ b/big\n@@ -1 +1 @@\n-' + 'x'.repeat(DIFF_CHAR_CAP) + '\n+y\n';
  const { files, cut } = parseDiff(big);
  assert.equal(cut, true);
  assert.deepEqual(files.map((f) => f.path), ['src/a.ts']);
});

test('rowAnchor: new side for add/ctx, old side for del, none for hunks', () => {
  assert.deepEqual(rowAnchor({ kind: 'add', new: 5, text: '' }), { line: 5, side: 'new' });
  assert.deepEqual(rowAnchor({ kind: 'ctx', old: 4, new: 5, text: '' }), { line: 5, side: 'new' });
  assert.deepEqual(rowAnchor({ kind: 'del', old: 4, text: '' }), { line: 4, side: 'old' });
  assert.equal(rowAnchor({ kind: 'hunk', text: '@@' }), null);
});

test('viewTakesComments: work and uncommitted only', () => {
  assert.equal(viewTakesComments('work'), true);
  assert.equal(viewTakesComments('uncommitted'), true);
  assert.equal(viewTakesComments('unpushed'), false);
});

test('fixItems: the server shape, repo/view only when not the default, code capped', () => {
  const cs: ReviewComment[] = [
    { text: 'overall' },
    { text: 'here', path: 'a.ts', line: 3, side: 'new', code: 'c'.repeat(600), repo: '.', view: 'work' },
    { text: 'there', path: 'b.ts', line: 7, side: 'old', code: 'x', repo: 'sub', view: 'uncommitted' },
  ];
  const out = fixItems(cs);
  assert.deepEqual(out[0], { text: 'overall' });
  assert.equal((out[1].code as string).length, 500);
  assert.equal('repo' in out[1], false);
  assert.equal('view' in out[1], false);
  assert.deepEqual(out[2], { path: 'b.ts', line: 7, side: 'old', code: 'x', text: 'there', repo: 'sub', view: 'uncommitted' });
});

test('putComment: adds, replaces on the same anchor, removes when emptied, refuses past the cap', () => {
  const a: ReviewComment = { text: 'one', path: 'a.ts', line: 1, side: 'new', repo: '.', view: 'work' };
  let cs = putComment([], a);
  assert.equal(cs.length, 1);
  cs = putComment(cs, { ...a, text: 'two' });
  assert.deepEqual(cs.map((c) => c.text), ['two']);
  cs = putComment(cs, { ...a, side: 'old', text: 'old side' });
  assert.equal(cs.length, 2);
  cs = putComment(cs, { ...a, text: '  ' });
  assert.deepEqual(cs.map((c) => c.text), ['old side']);
  assert.equal(putComment(cs, { text: '   ' }), cs);
  const full = Array.from({ length: FIX_MAX_COMMENTS }, (_, i) => ({ text: `c${i}` }));
  assert.equal(putComment(full, { text: 'one more' }), full);
  assert.equal(putComment([], { text: 'y'.repeat(FIX_MAX_CHARS + 10) })[0].text.length, FIX_MAX_CHARS);
});

test('reviewError: empty, too many, too long, blank, ok', () => {
  assert.match(reviewError([]), /Click a line/);
  assert.match(reviewError(Array.from({ length: FIX_MAX_COMMENTS + 1 }, () => ({ text: 'x' }))), /At most/);
  assert.match(reviewError([{ text: 'ok' }, { text: 'z'.repeat(FIX_MAX_CHARS + 1) }]), /Comment 2 is over/);
  assert.match(reviewError([{ text: ' ' }]), /empty/);
  assert.equal(reviewError([{ text: 'fine' }]), '');
});

test('commentWhere', () => {
  assert.equal(commentWhere({ text: 'x' }), 'the whole change');
  assert.equal(commentWhere({ text: 'x', path: 'a.ts', line: 3, side: 'new', repo: '.', view: 'work' }), 'a.ts:3');
  assert.equal(commentWhere({ text: 'x', path: 'a.ts', line: 3, side: 'old', repo: 'sub', view: 'uncommitted' }), 'sub: a.ts:3 (old), uncommitted');
});

test('gapAbove: first hunk from line 1, between hunks, none when adjacent, capped at LINES_CAP', () => {
  const rows = parseDiff(DIFF).files[0].rows;
  assert.deepEqual(gapAbove(rows, 0), { from: 1, to: 9 });
  const h2 = rows.findIndex((r, i) => i > 0 && r.kind === 'hunk');
  assert.deepEqual(gapAbove(rows, h2), { from: 14, to: 40 });
  assert.equal(gapAbove(rows, 1), null);
  const adj = parseDiff('--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n a\n-b\n+c\n').files[0].rows;
  assert.equal(gapAbove(adj, 0), null);
  const far = parseDiff('--- a/f\n+++ b/f\n@@ -2000,1 +2000,1 @@\n-b\n+c\n').files[0].rows;
  assert.deepEqual(gapAbove(far, 0), { from: 2000 - LINES_CAP, to: 1999 });
});

test('insertLines: context rows after the header with the hunk offset; the rest of the gap stays reachable', () => {
  const rows = parseDiff('--- a/f\n+++ b/f\n@@ -2000,1 +2001,1 @@\n-b\n+c\n').files[0].rows;
  const g = gapAbove(rows, 0)!;
  const lines = Array.from({ length: g.to - g.from + 1 }, (_, k) => `l${g.from + k}`);
  const next = insertLines(rows, 0, g.from, lines);
  assert.equal(next[0].kind, 'hunk');
  assert.deepEqual([next[1].kind, next[1].new, next[1].old, next[1].text], ['ctx', g.from, g.from - 1, `l${g.from}`]);
  assert.equal(next.length, rows.length + lines.length);
  assert.deepEqual(gapAbove(next, 0), { from: Math.max(1, g.from - LINES_CAP), to: g.from - 1 });
  assert.equal(insertLines(rows, 1, 1, ['x']), rows);
  assert.equal(insertLines(rows, 0, 1, []), rows);
});
