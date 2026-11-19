import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, readdirSync, statSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { loadJson, saveJson, shapeMerge } from './store';
import { defaultSettings, mergeSettings, coerceSettings, readEnv } from './settings';
import { defaultState, coerceState, mergeState } from './state';

const dir = () => mkdtempSync(join(tmpdir(), 'l2-store-'));

test('saveJson is atomic and mode 600; loadJson reads it back', () => {
  const d = dir(); const f = join(d, 's.json');
  saveJson(f, { a: 1 });
  assert.equal(statSync(f).mode & 0o777, 0o600);
  assert.deepEqual(readdirSync(d), ['s.json']);
  assert.deepEqual(loadJson(f, () => ({ a: 0 }), (r) => r as { a: number }).value, { a: 1 });
});
test('a corrupt file is moved aside and defaults load', () => {
  const d = dir(); const f = join(d, 's.json');
  writeFileSync(f, '{not json');
  const r = loadJson(f, () => ({ a: 0 }), (x) => x as { a: number });
  assert.deepEqual(r.value, { a: 0 });
  assert.ok(r.recovered && r.recovered.includes('.corrupt-'));
  assert.ok(readdirSync(d).some((n) => n.startsWith('s.json.corrupt-')));
  assert.equal(readFileSync(r.recovered!, 'utf8'), '{not json');
});
test('a missing file loads defaults without recovery', () => {
  const r = loadJson(join(dir(), 'none.json'), () => 5, () => null);
  assert.equal(r.value, 5); assert.equal(r.recovered, null);
});
test('shapeMerge drops unknown keys and type mismatches', () => {
  const b = { a: 1, s: 'x', o: { k: true }, n: null as string | null };
  const m = shapeMerge(b, { a: 'no', s: 'y', zz: 1, o: { k: false, extra: 1 }, n: 'set' });
  assert.deepEqual(m, { a: 1, s: 'y', o: { k: false }, n: 'set' });
});
test('settings: unknown keys dropped, enums and ranges enforced', () => {
  const s = mergeSettings(defaultSettings(), { bogus: 1, appearance: { theme: 'nope', uiScale: 99, density: 'compact' } } as never);
  assert.equal((s as unknown as Record<string, unknown>).bogus, undefined);
  assert.equal(s.appearance.theme, defaultSettings().appearance.theme);
  assert.equal(s.appearance.density, 'compact');
  assert.notEqual(s.appearance.uiScale, 99);
  assert.equal(coerceSettings('garbage'), null);
});
test('state: version must be 1', () => {
  assert.equal(coerceState({ version: 2 }), null);
  const s = mergeState(defaultState(), { window: { width: 1000 } } as never);
  assert.equal(s.window.width, 1000);
});
test('readEnv reads only HARNESS_DESKTOP_*', () => {
  const e = readEnv({ HARNESS_DESKTOP_SERVE_URL: 'http://127.0.0.1:1', HARNESS_DESKTOP_E2E: '1', HARNESS_DESKTOP_NO_UPDATE: '1', OTHER: 'x' });
  assert.equal(e.serveUrl, 'http://127.0.0.1:1');
  assert.equal(e.e2e, true);
  assert.equal(e.noUpdate, true);
});
