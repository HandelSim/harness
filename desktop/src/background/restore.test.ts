// review2 P1-5: a lost WebGL context that is never restored must not leave a blank layer forever.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { RestoreWatch, RESTORE_WAIT_MS } from './restore';

function rig(judge = () => true) {
  const timers: { f: () => void; ms: number; live: boolean }[] = [];
  const gaveUp: number[] = [];
  const w = new RestoreWatch({
    setTimeout: (f, ms) => { const h = { f, ms, live: true }; timers.push(h); return h; },
    clearTimeout: (h) => { (h as { live: boolean }).live = false; },
    canJudge: () => judge(), onGiveUp: (id) => gaveUp.push(id),
  });
  const fire = () => { const live = timers.filter((x) => x.live); for (const x of live) { x.live = false; x.f(); } return live.length; };
  return { w, timers, gaveUp, fire };
}

test('restore: no new frame within the wait -> give up on that layer', () => {
  const { w, timers, gaveUp, fire } = rig();
  w.arm(7, 120);
  assert.equal(timers[0].ms, RESTORE_WAIT_MS);
  w.frame(7, 120);                               // a stats report while lost (pause) carries the old count
  assert.equal(fire(), 1);
  assert.deepEqual(gaveUp, [7]);
  assert.equal(w.watching, -1);
});
test('restore: a frame past the loss clears the watch; other layers do not', () => {
  const { w, gaveUp, fire } = rig();
  w.arm(3, 10);
  w.frame(4, 50);
  assert.equal(w.watching, 3);
  w.frame(3, 11);
  assert.equal(w.watching, -1);
  fire();
  assert.deepEqual(gaveUp, []);
});
test('restore: while paused/hidden it waits instead of judging', () => {
  let ok = false;
  const { w, gaveUp, fire } = rig(() => ok);
  w.arm(1, 0);
  fire(); fire();
  assert.deepEqual(gaveUp, [], 'paused: no verdict');
  ok = true;
  fire();
  assert.deepEqual(gaveUp, [1]);
});
test('restore: clear() and re-arm cancel the old timer', () => {
  const { w, timers, gaveUp, fire } = rig();
  w.arm(1, 0); w.arm(2, 0);
  assert.equal(timers.filter((x) => x.live).length, 1);
  fire();
  assert.deepEqual(gaveUp, [2]);
  w.arm(5, 0); w.clear(); fire();
  assert.deepEqual(gaveUp, [2]);
});
