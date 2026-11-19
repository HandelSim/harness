// Settings validation: accent is a preset enum (review2 P1-1); an old hex accent loads as the default preset.
import test from 'node:test';
import assert from 'node:assert/strict';
import { defaultSettings, mergeSettings, coerceSettings } from './settings';
import { ACCENT_PRESETS } from '../shared/ipc';

test('accent: default is the iris preset', () => {
  assert.equal(defaultSettings().appearance.accent, 'iris');
  assert.ok((ACCENT_PRESETS as readonly string[]).includes(defaultSettings().appearance.accent));
});
test('accent: every preset round-trips through a patch', () => {
  for (const a of ACCENT_PRESETS) assert.equal(mergeSettings(defaultSettings(), { appearance: { accent: a } }).appearance.accent, a);
});
test('accent: a bad patch keeps the current preset', () => {
  const cur = mergeSettings(defaultSettings(), { appearance: { accent: 'teal' } });
  assert.equal(mergeSettings(cur, { appearance: { accent: '#ff0000' } }).appearance.accent, 'teal');
  assert.equal(mergeSettings(cur, { appearance: { accent: 'nope' } }).appearance.accent, 'teal');
});
test('accent: a stored hex accent (old settings.json) loads cleanly as iris', () => {
  const s = coerceSettings({ appearance: { theme: 'light', accent: '#7c9cff', seeThrough: 0.3 } });
  assert.ok(s);
  assert.equal(s.appearance.accent, 'iris');
  assert.equal(s.appearance.theme, 'light');
  assert.equal(s.appearance.seeThrough, 0.3);
});
test('enum repair falls back to the default when the fallback itself is invalid', () => {
  const bad = defaultSettings();
  (bad.appearance as { accent: string }).accent = '#123456';
  (bad.appearance as { theme: string }).theme = 'neon';
  const s = mergeSettings(bad, {});
  assert.equal(s.appearance.accent, 'iris');
  assert.equal(s.appearance.theme, 'dark');
});
