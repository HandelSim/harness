import { test } from 'node:test';
import assert from 'node:assert/strict';
import { classifyRenderer, lowPowerActive, lowPowerPlan, isLowPowerMode, LOW_POWER_FPS } from './gpu';
import { CATALOG } from './catalog';

test('gpu: software rasterisers are classified as software', () => {
  for (const r of [
    'ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero) (0x0000C0DE)), SwiftShader driver)',
    'Google SwiftShader', 'llvmpipe (LLVM 15.0.7, 256 bits)', 'Mesa softpipe', 'Microsoft Basic Render Driver',
    'ANGLE (Microsoft, Microsoft Basic Render Driver Direct3D11 vs_5_0 ps_5_0)', 'Software Rasterizer',
  ]) assert.equal(classifyRenderer(r).software, true, r);
});
test('gpu: hardware renderers are not', () => {
  for (const r of [
    'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)', 'Apple M2', 'Mesa Intel(R) UHD Graphics 620 (KBL GT2)',
    'AMD Radeon RX 6800 (radeonsi, navi21, LLVM 15.0.7, DRM 3.49)', 'Mali-G78', 'WebKit WebGL',
  ]) assert.equal(classifyRenderer(r).software, false, r);
});
test('gpu: no context means software; renderer strings are bounded', () => {
  assert.deepEqual(classifyRenderer('', false), { renderer: 'none', software: true });
  assert.equal(classifyRenderer(null).software, false);
  assert.equal(classifyRenderer('x'.repeat(5000)).renderer.length, 256);
});
test('gpu: mode resolution', () => {
  const sw = { renderer: 'SwiftShader', software: true }, hw = { renderer: 'Apple M2', software: false };
  assert.equal(lowPowerActive('auto', sw), true);
  assert.equal(lowPowerActive('auto', hw), false);
  assert.equal(lowPowerActive('on', hw), true);
  assert.equal(lowPowerActive('off', sw), false);
  assert.ok(isLowPowerMode('auto') && isLowPowerMode('on') && isLowPowerMode('off'));
  assert.ok(!isLowPowerMode('yes') && !isLowPowerMode(undefined));
});
test('gpu: plan per catalog entry', () => {
  for (const d of CATALOG) {
    const p = lowPowerPlan(d);
    if (!d.dynamic) assert.deepEqual(p, { mode: 'full', fpsCap: 60, scaleMul: 1 }, d.id);
    else if (d.id === 'tracers' || d.id === 'grayscott') assert.equal(p.mode, 'static', d.id);
    else { assert.equal(p.mode, 'reduced', d.id); assert.equal(p.fpsCap, LOW_POWER_FPS); assert.ok(p.scaleMul > 0 && p.scaleMul < 1); }
  }
  assert.ok(CATALOG.some((d) => lowPowerPlan(d).mode === 'reduced'), 'some dynamic ids stay animated');
});
