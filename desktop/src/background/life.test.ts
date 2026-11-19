import { test } from 'node:test';
import assert from 'node:assert/strict';
import { lifeStep, stamp, GLIDER } from './effects/life';

const grid = (w: number, h: number, cells: [number, number][]) => { const g = new Uint8Array(w * h); for (const [x, y] of cells) g[y * w + x] = 1; return g; };
const cellsOf = (g: Uint8Array, w: number) => Array.from(g).flatMap((v, i) => (v ? [[i % w, Math.floor(i / w)] as [number, number]] : [])).sort((a, b) => a[1] - b[1] || a[0] - b[0]);

test('life: blinker oscillates with period 2', () => {
  const w = 5, h = 5;
  const a = grid(w, h, [[1, 2], [2, 2], [3, 2]]), b = new Uint8Array(w * h), c = new Uint8Array(w * h);
  assert.equal(lifeStep(a, b, w, h), 3);
  assert.deepEqual(cellsOf(b, w), [[2, 1], [2, 2], [2, 3]]);
  lifeStep(b, c, w, h);
  assert.deepEqual(cellsOf(c, w), cellsOf(a, w));
});
test('life: glider moves by (1,1) every 4 generations', () => {
  const w = 10, h = 10;
  let a = new Uint8Array(w * h), b = new Uint8Array(w * h);
  stamp(a, w, h, GLIDER, 1, 1);
  const start = cellsOf(a, w);
  for (let i = 0; i < 4; i++) { lifeStep(a, b, w, h); [a, b] = [b, a]; }
  assert.deepEqual(cellsOf(a, w), start.map(([x, y]) => [x + 1, y + 1]).sort((p, q) => p[1] - q[1] || p[0] - q[0]));
});
test('life: the grid wraps (a glider crosses the edge and keeps its 5 cells)', () => {
  const w = 8, h = 8;
  let a = new Uint8Array(w * h), b = new Uint8Array(w * h);
  stamp(a, w, h, GLIDER, 5, 5);
  for (let i = 0; i < 4 * w; i++) { lifeStep(a, b, w, h); [a, b] = [b, a]; }
  // after 4*w generations the glider has moved (w, w): back where it started on the torus
  const ref = new Uint8Array(w * h);
  stamp(ref, w, h, GLIDER, 5, 5);
  assert.deepEqual(cellsOf(a, w), cellsOf(ref, w));
});
