import { test } from 'node:test';
import assert from 'node:assert/strict';
import { CATALOG, DEFAULT_BG, normalizeConfig, defaultParams, descriptor } from './catalog';
import { EFFECTS } from './effects/registry';

test('catalog: ids unique; every dynamic id has an effect; static ones have none', () => {
  const ids = CATALOG.map((d) => d.id);
  assert.equal(new Set(ids).size, ids.length);
  for (const d of CATALOG) {
    if (d.dynamic) assert.ok(EFFECTS[d.id], `no effect for ${d.id}`);
    else { assert.equal(EFFECTS[d.id], undefined); assert.equal(d.renderer, 'css'); }
    if (d.renderer === 'webgl2') assert.ok(EFFECTS[d.id].makeGL, `${d.id} says webgl2 but has no GL path`);
    if (d.renderer === 'worker2d') assert.ok(EFFECTS[d.id].make2d);
  }
});
test('catalog: ships the v1 set', () => {
  for (const id of ['solid', 'mesh', 'grid', 'silk', 'flow', 'attractor', 'tracers', 'grayscott']) assert.ok(descriptor(id), id);
});
test('catalog: every default is within its range / options', () => {
  for (const d of CATALOG) for (const p of d.params) {
    if (p.type === 'range') {
      assert.equal(typeof p.default, 'number');
      assert.ok(p.min! <= (p.default as number) && (p.default as number) <= p.max!, `${d.id}.${p.key}`);
      assert.ok(p.step! > 0);
    } else if (p.type === 'select') assert.ok(p.options!.some((o) => o.value === p.default), `${d.id}.${p.key}`);
    else assert.equal(typeof p.default, 'boolean');
  }
});
test('catalog: DEFAULT_BG is valid, static and normalises to itself', () => {
  assert.equal(descriptor(DEFAULT_BG.id)!.dynamic, false);
  assert.deepEqual(normalizeConfig(DEFAULT_BG), DEFAULT_BG);
  assert.deepEqual(DEFAULT_BG.params, defaultParams(descriptor(DEFAULT_BG.id)!));
});
test('catalog: normalizeConfig repairs junk', () => {
  assert.deepEqual(normalizeConfig(null), DEFAULT_BG);
  assert.deepEqual(normalizeConfig({ id: 'nope' }), DEFAULT_BG);
  const c = normalizeConfig({ id: 'flow', seed: -5, intensity: 9, speed: 'x', fpsCap: 45, params: { particles: 1e9, zoom: '1.5', junk: 1, trail: NaN } });
  assert.equal(c.id, 'flow');
  assert.equal(c.seed, 0);
  assert.equal(c.intensity, 1);
  assert.equal(c.speed, DEFAULT_BG.speed);
  assert.equal(c.fpsCap, DEFAULT_BG.fpsCap);
  assert.equal(c.params.particles, 4000);
  assert.equal(c.params.zoom, 1.5);
  assert.equal(c.params.trail, 0.92);
  assert.equal('junk' in c.params, false);
  const s = normalizeConfig({ id: 'attractor', params: { map: 'evil', morph: 'yes' } });
  assert.equal(s.params.map, 'clifford');
  assert.equal(s.params.morph, true);
});
