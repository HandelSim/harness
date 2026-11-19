import { test } from 'node:test';
import assert from 'node:assert/strict';
import { simplex3, valueNoise2, fbm2, curl2 } from './noise';

test('noise: simplex3 in [-1,1] and seeded', () => {
  const a = simplex3(1), b = simplex3(1), c = simplex3(2);
  let diff = 0;
  for (let i = 0; i < 5000; i++) {
    const x = i * 0.137, y = i * 0.071, z = i * 0.013;
    const v = a(x, y, z);
    assert.ok(v >= -1 && v <= 1, `out of range ${v}`);
    assert.equal(v, b(x, y, z));
    if (Math.abs(v - c(x, y, z)) > 1e-6) diff++;
  }
  assert.ok(diff > 4000);
});
test('noise: simplex3 is continuous', () => {
  const n = simplex3(9);
  for (let i = 0; i < 2000; i++) {
    const x = i * 0.31, y = i * 0.17, z = 0.5;
    assert.ok(Math.abs(n(x, y, z) - n(x + 1e-4, y, z)) < 1e-2);
  }
});
test('noise: value noise and fbm in [0,1], continuous, seeded', () => {
  const n = valueNoise2(3), m = valueNoise2(3), o = valueNoise2(4);
  let diff = 0;
  for (let i = 0; i < 5000; i++) {
    const x = i * 0.093 - 50, y = i * 0.057 - 30;
    const v = n(x, y), f = fbm2(n, x, y);
    assert.ok(v >= 0 && v <= 1 && f >= 0 && f <= 1, `${v} ${f}`);
    assert.equal(v, m(x, y));
    assert.ok(Math.abs(v - n(x + 1e-4, y)) < 1e-3);
    if (Math.abs(v - o(x, y)) > 1e-6) diff++;
  }
  assert.ok(diff > 4000);
});
test('noise: curl field is divergence-free (numerically)', () => {
  const n = simplex3(11);
  const e = 1e-3;
  for (let i = 0; i < 200; i++) {
    const x = i * 0.37, y = i * 0.23, t = 0.4;
    const div = (curl2(n, x + e, y, t)[0] - curl2(n, x - e, y, t)[0]) / (2 * e) + (curl2(n, x, y + e, t)[1] - curl2(n, x, y - e, t)[1]) / (2 * e);
    const mag = Math.hypot(...curl2(n, x, y, t));
    assert.ok(Math.abs(div) < 0.05 * Math.max(1, mag), `div ${div}`);
  }
});
