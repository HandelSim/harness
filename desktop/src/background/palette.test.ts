import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseColor, derivePalette, luminance, ramp, toCss, rampLut } from './palette';

const close = (a: number[] | null, b: number[]) => { assert.ok(a); a!.forEach((v, i) => assert.ok(Math.abs(v - b[i]) < 1e-3, `${a} vs ${b}`)); };

test('palette: parses hex, rgb(a) in both syntaxes, hsl', () => {
  close(parseColor('#fff'), [1, 1, 1]);
  close(parseColor('#0b0c0e'), [11 / 255, 12 / 255, 14 / 255]);
  close(parseColor('#5b5bd680'), [0x5b / 255, 0x5b / 255, 0xd6 / 255]);
  close(parseColor('#abcd'), [0xaa / 255, 0xbb / 255, 0xcc / 255]);
  close(parseColor('rgb(91, 91, 214)'), [91 / 255, 91 / 255, 214 / 255]);
  close(parseColor('rgba(255,0,0,0.5)'), [1, 0, 0]);
  close(parseColor('rgb(0 128 255 / 50%)'), [0, 128 / 255, 1]);
  close(parseColor('rgb(100%, 0%, 50%)'), [1, 0, 0.5]);
  close(parseColor('hsl(0, 100%, 50%)'), [1, 0, 0]);
  close(parseColor('  HSL(120 100% 25%) '), [0, 0.5, 0]);
});
test('palette: rejects garbage', () => {
  for (const s of ['', 'red', '#12', '#12345', 'rgb(1,2)', 'rgb(a,b,c)', 'oklch(0.5 0.1 200)', 'url(x)']) assert.equal(parseColor(s), null, s);
  assert.equal(parseColor(undefined as unknown as string), null);
});
test('palette: dark ramp stays dark and low-saturation; light caps intensity', () => {
  const d = derivePalette({ mode: 'dark', base: '#0b0c0e', fg: '#b0b4ba', accent: '#5b5bd6', accent2: '#b1a9ff' });
  assert.ok(luminance(d.c1) < 0.05, `c1 ${luminance(d.c1)}`);
  assert.ok(luminance(d.c2) < 0.2, `c2 ${luminance(d.c2)}`);
  assert.equal(d.maxIntensity, 1);
  const l = derivePalette({ mode: 'light', base: '#eceef2', fg: '#1c2024', accent: '#5b5bd6', accent2: '#9b9ef0' });
  assert.equal(l.maxIntensity, 0.35);
  assert.ok(luminance(l.c0) > 0.7);
});
test('palette: a malformed theme falls back to defaults instead of throwing', () => {
  const p = derivePalette({ mode: 'dark', base: 'nope', fg: '', accent: 'var(--x)', accent2: '' });
  for (const c of [p.c0, p.c1, p.c2, p.c3]) c.forEach((v) => assert.ok(Number.isFinite(v) && v >= 0 && v <= 1));
  assert.ok(derivePalette(null));
});
test('palette: ramp endpoints, css output, lut size', () => {
  const p = derivePalette(null);
  assert.deepEqual(ramp(p, 0), p.c0);
  close(ramp(p, 1), p.c3);
  assert.equal(toCss([1, 0, 0]), 'rgb(255, 0, 0)');
  assert.equal(toCss([0, 0, 0], 0.5), 'rgba(0, 0, 0, 0.5)');
  assert.equal(rampLut(p).length, 1024);
});
