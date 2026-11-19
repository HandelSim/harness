import { test } from 'node:test';
import assert from 'node:assert/strict';
import { gsCreate, gsStep } from './effects/grayscott';
import { GRAYSCOTT_PRESETS } from './catalog';
import { mulberry32 } from './prng';

test('grayscott: every shipped preset stays in [0,1] with no NaN after 500 steps on 32x32', () => {
  for (const [name, [F, K]] of Object.entries(GRAYSCOTT_PRESETS)) {
    const g = gsCreate(32, 32, mulberry32(1), 3);
    for (let i = 0; i < 500; i++) gsStep(g, F, K);
    for (let i = 0; i < g.A.length; i++) {
      assert.ok(g.A[i] >= 0 && g.A[i] <= 1 && g.B[i] >= 0 && g.B[i] <= 1, `${name} out of range at ${i}`);
      assert.ok(!Number.isNaN(g.A[i]) && !Number.isNaN(g.B[i]));
    }
  }
});
test('grayscott: the coral preset keeps a pattern alive (B neither vanishes nor fills)', () => {
  const [F, K] = GRAYSCOTT_PRESETS.coral;
  const g = gsCreate(64, 64, mulberry32(2), 6);
  for (let i = 0; i < 1500; i++) gsStep(g, F, K);
  let sum = 0;
  for (const b of g.B) sum += b;
  const mean = sum / g.B.length;
  assert.ok(mean > 0.01 && mean < 0.6, `mean B ${mean}`);
});
test('grayscott: deterministic for a seed', () => {
  const a = gsCreate(16, 16, mulberry32(5), 2), b = gsCreate(16, 16, mulberry32(5), 2);
  for (let i = 0; i < 50; i++) { gsStep(a, 0.05, 0.063); gsStep(b, 0.05, 0.063); }
  assert.deepEqual(Array.from(a.B), Array.from(b.B));
});
