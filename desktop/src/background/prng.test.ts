import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mulberry32, deriveSeed, range, int } from './prng';

test('prng: same seed gives the same sequence', () => {
  const a = mulberry32(42), b = mulberry32(42);
  for (let i = 0; i < 1000; i++) assert.equal(a(), b());
});
test('prng: different seeds diverge', () => {
  const a = mulberry32(1), b = mulberry32(2);
  let same = 0;
  for (let i = 0; i < 100; i++) if (a() === b()) same++;
  assert.ok(same < 2);
});
test('prng: values in [0,1), roughly uniform', () => {
  const r = mulberry32(7);
  const bins = new Array(10).fill(0);
  for (let i = 0; i < 100000; i++) { const v = r(); assert.ok(v >= 0 && v < 1); bins[Math.floor(v * 10)]++; }
  for (const b of bins) assert.ok(b > 9000 && b < 11000, `bin ${b}`);
});
test('prng: seed 0 and huge seeds work; helpers stay in range', () => {
  const r0 = mulberry32(0), rh = mulberry32(0xffffffff);
  assert.ok(Number.isFinite(r0()) && Number.isFinite(rh()));
  const r = mulberry32(3);
  for (let i = 0; i < 1000; i++) {
    const x = range(r, -2, 5); assert.ok(x >= -2 && x < 5);
    const n = int(r, 3, 9); assert.ok(Number.isInteger(n) && n >= 3 && n < 9);
  }
});
test('prng: deriveSeed is deterministic and label-dependent', () => {
  assert.equal(deriveSeed(5, 'flow'), deriveSeed(5, 'flow'));
  assert.notEqual(deriveSeed(5, 'flow'), deriveSeed(5, 'life'));
  assert.notEqual(deriveSeed(5, 'flow'), deriveSeed(6, 'flow'));
});
