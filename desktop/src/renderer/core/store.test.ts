import { test } from 'node:test';
import assert from 'node:assert/strict';
import { effect } from '@preact/signals-core';
import { TopicStore } from './store';

function mk() {
  const pushed: string[][] = [];
  const s = new TopicStore({ setTopics: (t) => pushed.push(t), fetch: async () => ({ ok: true, status: 200, sig: '', data: undefined, same: true }) as never, now: () => 1 },
    (fn) => fn());
  return { s, pushed };
}

test('TopicStore.apply (M1): a re-send with the same sig keeps data identity, so subscribers do not re-render', () => {
  const { s } = mk();
  const q = s.entry('queue');
  const t = s.entry('ticket:A-1');
  let qRenders = 0, tRenders = 0;
  const stopQ = effect(() => { void q.data.value; void q.at.value; void q.stale.value; void q.error.value; qRenders++; });
  const stopT = effect(() => { void t.data.value; void t.at.value; tRenders++; });
  s.apply('queue', 'q1', { tickets: [1, 2, 3] });
  assert.equal(qRenders, 2);
  const kept = q.data.value;
  for (let i = 0; i < 10; i++) s.apply('queue', 'q1', { tickets: [1, 2, 3] });   // IPC clones: new identity, same sig
  assert.equal(qRenders, 2, 'same sig: no re-render');
  assert.equal(q.data.value, kept);
  assert.equal(tRenders, 1, 'an unrelated topic never re-renders');
  s.apply('queue', 'q2', { tickets: [1, 2] });
  assert.ok(qRenders > 2, 'a new sig renders');
  stopQ(); stopT();
});

test('TopicStore: an error marks loaded data stale once; a same-sig re-send clears it', () => {
  const { s } = mk();
  const q = s.entry('queue');
  s.apply('queue', 'q1', { n: 1 });
  let errRenders = 0;
  const stop = effect(() => { void q.error.value; errRenders++; });
  s.fail('queue', { code: 'internal', message: 'boom' });
  s.fail('queue', { code: 'internal', message: 'boom' });
  assert.equal(errRenders, 2, 'an identical error does not re-render');
  assert.equal(q.stale.value, true);
  s.apply('queue', 'q1', { n: 1 });
  assert.equal(q.stale.value, false);
  assert.equal(q.error.value, null);
  stop();
});
