import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  mergeAnswers, answerBody, unansweredBlocking, validateComments, addComment, appendOut, emptyTail, pickOut, timeline,
  liveSession, defaultTab, statusTone, visibleBarActions, isStopping, optionLabel, FIX_MAX_CHARS, FIX_MAX_COMMENTS,
  type Question, type FixComment,
} from './model';
import type { Gate, TicketAction, TicketState } from '../../../shared/api';

const q = (id: string, text = `Q ${id}`, extra: Partial<Question> = {}): Question =>
  ({ id, text, ask: '', blocking: false, default: '', options: [], ...extra });

test('mergeAnswers keeps typed answers for surviving questions and marks changed ones', () => {
  const prev = [q('a'), q('b'), q('c')];
  const draft = { answers: { a: 'yes', b: 'no', c: 'gone answer' }, feedback: 'fb' };
  const next = [q('a'), q('b', 'Q b reworded'), q('d')];
  const r = mergeAnswers(prev, draft, next);
  assert.deepEqual(r.draft.answers, { a: 'yes', b: 'no' });
  assert.equal(r.draft.feedback, 'fb');
  assert.deepEqual(r.changed.sort(), ['b', 'd']);
  assert.deepEqual(r.orphans, [{ id: 'c', text: 'Q c', answer: 'gone answer' }]);
});

test('mergeAnswers on first load marks nothing changed', () => {
  const r = mergeAnswers([], { answers: {}, feedback: '' }, [q('a')]);
  assert.deepEqual(r.changed, []);
  assert.deepEqual(r.orphans, []);
});

test('mergeAnswers detects option/default changes and ignores blank orphans', () => {
  const prev = [q('a', 'A', { options: ['x', 'y'] }), q('b')];
  const next = [q('a', 'A', { options: ['x', 'z'] })];
  const r = mergeAnswers(prev, { answers: { a: '', b: '   ' }, feedback: '' }, next);
  assert.deepEqual(r.changed, ['a']);
  assert.deepEqual(r.orphans, []);
  assert.deepEqual(r.draft.answers, { a: '' });
});

test('answerBody sends only current, non-empty answers plus the plan sig', () => {
  const body = answerBody([q('a'), q('b')], { answers: { a: ' yes ', b: '  ', z: 'orphan' }, feedback: '' }, 'sig1');
  assert.deepEqual(body, { answers: { a: 'yes' }, sig: 'sig1' });
  const b2 = answerBody([q('a')], { answers: {}, feedback: ' change it ' }, 's', { defaults: true, go: true });
  assert.deepEqual(b2, { answers: {}, sig: 's', feedback: 'change it', defaults: true, go: true });
});

test('unansweredBlocking ignores questions with a default', () => {
  const qs = [q('a', 'A', { blocking: true }), q('b', 'B', { blocking: true, default: 'x' }), q('c')];
  assert.deepEqual(unansweredBlocking(qs, { answers: {}, feedback: '' }), ['a']);
  assert.deepEqual(unansweredBlocking(qs, { answers: { a: 'ok' }, feedback: '' }), []);
});

test('validateComments enforces 40 x 2000', () => {
  const c = (text: string): FixComment => ({ text });
  assert.equal(validateComments([]), 'Add at least one comment.');
  assert.equal(validateComments([c('fine')]), '');
  assert.match(validateComments(Array.from({ length: FIX_MAX_COMMENTS + 1 }, () => c('x'))), /At most 40/);
  assert.match(validateComments([c('ok'), c('y'.repeat(FIX_MAX_CHARS + 1))]), /Comment 2 is 2001 characters/);
  assert.equal(validateComments([c('y'.repeat(FIX_MAX_CHARS))]), '');
  assert.match(validateComments([c('  ')]), /empty/);
});

test('addComment refuses past the cap and clamps text', () => {
  let cs: FixComment[] = [];
  for (let i = 0; i < 45; i++) cs = addComment(cs, { text: 'z'.repeat(2500) });
  assert.equal(cs.length, 40);
  assert.equal(cs[0].text.length, FIX_MAX_CHARS);
});

test('appendOut appends in order, ignores stale/out-of-order chunks, handles reset and cap', () => {
  let t = emptyTail('work-1.out');
  t = appendOut(t, { name: 'work-1.out', from: 0, next: 5, size: 5, text: 'hello', reset: false });
  t = appendOut(t, { name: 'work-1.out', from: 5, next: 11, size: 11, text: ' world', reset: false });
  assert.equal(t.text, 'hello world');
  assert.equal(t.next, 11);
  const same = appendOut(t, { name: 'work-1.out', from: 5, next: 11, size: 11, text: ' world', reset: false });
  assert.equal(same, t);
  const other = appendOut(t, { name: 'plan-1.out', from: 11, next: 12, size: 12, text: 'x', reset: false });
  assert.equal(other, t);
  const r = appendOut(t, { name: 'work-1.out', from: 0, next: 3, size: 3, text: 'new', reset: true });
  assert.equal(r.text, 'new');
  const capped = appendOut(emptyTail('a'), { name: 'a', from: 0, next: 10, size: 10, text: '0123456789', reset: false }, 4);
  assert.equal(capped.text, '6789');
  assert.equal(capped.truncated, true);
});

test('pickOut prefers the newest file of the current stage', () => {
  const outs = [{ name: 'plan-1.out', mtime: 1, size: 1 }, { name: 'work-1.out', mtime: 2, size: 1 }, { name: 'work-2.out', mtime: 3, size: 1 }, { name: 'verify-1.out', mtime: 4, size: 1 }];
  assert.equal(pickOut(outs, 'work'), 'work-2.out');
  assert.equal(pickOut(outs, 'push'), 'verify-1.out');
  assert.equal(pickOut([], 'work'), '');
});

test('timeline orders stages by first appearance and marks the running stage active', () => {
  const s: Pick<TicketState, 'history' | 'stage' | 'status'> = {
    status: 'running', stage: 'review',
    history: [
      { at: 1, stage: 'plan', attempt: 1, result: 'ok', details: '' },
      { at: 2, stage: 'work', attempt: 1, result: 'failed', details: '' },
      { at: 3, stage: 'work', attempt: 2, result: 'ok', details: '' },
    ],
  };
  assert.deepEqual(timeline(s), [
    { stage: 'plan', state: 'done', attempts: 1 },
    { stage: 'work', state: 'done', attempts: 2 },
    { stage: 'review', state: 'active', attempts: 0 },
  ]);
  const blocked = timeline({ status: 'blocked', stage: 'verify', history: [{ at: 1, stage: 'verify', attempt: 1, result: 'error', details: '' }] });
  assert.deepEqual(blocked, [{ stage: 'verify', state: 'failed', attempts: 1 }]);
});

test('liveSession picks the working session of the current stage', () => {
  const sessions = [
    { sid: 's1', stage: 'plan', label: '', kind: 'stage', working: false, created: 1 },
    { sid: 's2', stage: 'work', label: '', kind: 'stage', working: true, created: 2 },
    { sid: 's3', stage: 'work', label: '', kind: 'debug', working: false, created: 3 },
  ];
  assert.equal(liveSession({ sessions, state: { stage: 'work' } as TicketState })?.sid, 's2');
  assert.equal(liveSession({ sessions: sessions.map((x) => ({ ...x, working: false })), state: { stage: 'work' } as TicketState })?.sid, 's3');
  assert.equal(liveSession({ sessions: [], state: { stage: 'work' } as TicketState }), null);
});

test('status helpers', () => {
  assert.equal(defaultTab('plan-review'), 'plan');
  assert.equal(defaultTab('needs-answers'), 'plan');
  assert.equal(defaultTab('running'), 'live');
  assert.equal(defaultTab('done'), 'overview');
  assert.equal(statusTone('blocked'), 'err');
  assert.equal(statusTone('weird'), 'default');
  assert.equal(isStopping({ stopping: true } as unknown as TicketState), true);
  assert.equal(isStopping({} as TicketState), false);
  assert.deepEqual(optionLabel({ value: 'a', label: 'Alpha' }), { value: 'a', label: 'Alpha' });
  assert.deepEqual(optionLabel('b'), { value: 'b', label: 'b' });
});

test('visibleBarActions shows the archive toggle matching the server flag', () => {
  const g = (ok: boolean): Gate => ({ ok, reason: ok ? '' : 'no' });
  const actions = Object.fromEntries(['answer', 'approve', 'retry', 'continue', 'stop', 'remove', 'move', 'note', 'followup', 'edit', 'fix', 'archive', 'unarchive']
    .map((a) => [a, g(a !== 'approve')])) as Record<TicketAction, Gate>;
  const v = visibleBarActions({ archived: false, actions });
  assert.ok(v.includes('archive') && !v.includes('unarchive') && !v.includes('approve'));
  const w = visibleBarActions({ archived: true, actions: { ...actions, approve: g(true) } });
  assert.ok(w.includes('unarchive') && !w.includes('archive') && w.includes('approve'));
});

test('splitComments splits on blank lines and drops empties', async () => {
  const { splitComments } = await import('./model');
  assert.deepEqual(splitComments('fix a\nmore a\n\n  \nfix b\n\n'), [{ text: 'fix a\nmore a' }, { text: 'fix b' }]);
  assert.deepEqual(splitComments('   '), []);
});

test('pass-through readers', async () => {
  const { diffstatText, safeUrl, rec } = await import('./model');
  assert.equal(diffstatText({ files: 3, insertions: 10, deletions: 2 }), '3 files, +10 −2');
  assert.equal(diffstatText({ files: 0, insertions: 0, deletions: 0 }), '');
  assert.equal(diffstatText(undefined), '');
  assert.equal(safeUrl('javascript:alert(1)'), '');
  assert.equal(safeUrl('https://x.test/compare'), 'https://x.test/compare');
  assert.equal(rec([1]), null);
});
