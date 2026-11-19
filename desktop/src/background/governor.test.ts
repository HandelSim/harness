import { test } from 'node:test';
import assert from 'node:assert/strict';
import { Governor, LADDER } from './governor';

const run = (g: Governor, ms: number, from: number, to: number, fps = 60) => {
  let steps = 0;
  for (let t = from; t <= to; t += 1000 / fps) if (g.sample(ms, t)) steps++;
  return steps;
};

test('governor: under budget never downgrades', () => {
  const g = new Governor({ budgetMs: 4 });
  assert.equal(run(g, 5.9, 0, 60_000), 0);
  assert.equal(g.level, 0);
  assert.equal(g.downgraded, false);
});
test('governor: over 1.5x budget for less than 2 s does nothing; at 2 s steps once', () => {
  const g = new Governor({ budgetMs: 4 });
  run(g, 10, 0, 1800);
  assert.equal(g.level, 0);
  run(g, 10, 1800 + 16, 2400);
  assert.equal(g.level, 1);
  assert.equal(g.rung.scale, 0.5);
  assert.match(g.reason, /over/);
});
test('governor: walks the whole ladder (scale, then fps, then static) and stops', () => {
  const g = new Governor({ budgetMs: 4 });
  run(g, 50, 0, 60_000);
  assert.equal(g.level, LADDER.length - 1);
  assert.equal(g.rung.static, true);
  assert.equal(g.rung.tier, 3);
  const scales = LADDER.map((r) => r.scale), fps = LADDER.map((r) => r.fps);
  assert.deepEqual(scales.slice(0, 4), [1, 0.5, 0.35, 0.25]);
  assert.deepEqual(fps.slice(4, 6), [30, 15]);
  assert.equal(run(g, 50, 60_000, 70_000), 0);
});
test('governor: never climbs back by itself (no oscillation)', () => {
  const g = new Governor({ budgetMs: 4 });
  run(g, 12, 0, 2500);
  assert.equal(g.level, 1);
  run(g, 0.5, 2500, 120_000);
  assert.equal(g.level, 1);
});
test('governor: a spike shorter than the hold resets the hold', () => {
  const g = new Governor({ budgetMs: 4 });
  // 1 s spikes; the 60-frame mean lags ~0.7 s, so each over-budget stretch stays under the 2 s hold
  for (let k = 0; k < 20; k++) { run(g, 20, k * 3000, k * 3000 + 1000); run(g, 0.1, k * 3000 + 1016, k * 3000 + 2999); }
  assert.equal(g.level, 0);
});
test('governor: pause() drops the hold timer; reset() returns to the top', () => {
  const g = new Governor({ budgetMs: 4 });
  run(g, 10, 0, 1500);
  g.pause();
  run(g, 10, 100_000, 101_500);           // after a long pause: hold restarts, so still level 0
  assert.equal(g.level, 0);
  run(g, 10, 101_516, 103_000);
  assert.equal(g.level, 1);
  g.reset();
  assert.equal(g.level, 0);
  assert.equal(g.reason, '');
});
test('governor: bad samples are ignored; explicit downgrade works', () => {
  const g = new Governor({ budgetMs: 4 });
  assert.equal(g.sample(NaN, 0), false);
  assert.equal(g.sample(-1, 0), false);
  assert.equal(g.downgrade('context lost'), true);
  assert.equal(g.level, 1);
  assert.equal(g.reason, 'context lost');
});
