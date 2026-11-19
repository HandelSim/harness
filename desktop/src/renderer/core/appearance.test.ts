// review2 P1-1 / P1-2: bindAppearance guards the accent attribute and re-runs on OS theme / motion changes,
// bumping appearanceTick so the background engine and the glass contrast floor follow (core/background.ts).
import { test } from 'node:test';
import assert from 'node:assert/strict';

type Listener = () => void;
function mql(matches: boolean) {
  const ls: Listener[] = [];
  return { matches, addEventListener: (_: string, f: Listener) => { ls.push(f); }, fire() { for (const f of ls) f(); } };
}
const mq = mql(false), rm = mql(false);
const dataset: Record<string, string> = {};
const g = globalThis as Record<string, unknown>;
g.window = { matchMedia: (q: string) => (q.includes('color-scheme') ? mq : rm) };
g.document = { documentElement: { dataset, style: {} } };

test('appearance: OS theme and reduced-motion flips rewrite the attributes and bump appearanceTick', async () => {
  const { bindAppearance, settings, appearanceTick } = await import('./app');
  const { defaultSettings } = await import('../../main/settings');
  const s = defaultSettings();
  s.appearance.theme = 'system';
  settings.value = s;
  bindAppearance();
  assert.equal(dataset.theme, 'dark');
  assert.equal(dataset.accent, 'iris');
  assert.equal(dataset.reduceMotion, '0');
  const t0 = appearanceTick.value;
  mq.matches = true; mq.fire();
  assert.equal(dataset.theme, 'light');
  assert.ok(appearanceTick.value > t0, 'OS theme flip must reach the engine and the floor');
  const t1 = appearanceTick.value;
  rm.matches = true; rm.fire();
  assert.equal(dataset.reduceMotion, '1');
  assert.ok(appearanceTick.value > t1, 'OS reduce-motion must reach the engine');
  // a stored value outside the presets never blanks the --accent-* tokens
  settings.value = { ...s, appearance: { ...s.appearance, accent: '#7c9cff' } };
  assert.equal(dataset.accent, 'iris');
  settings.value = { ...s, appearance: { ...s.appearance, accent: 'teal' } };
  assert.equal(dataset.accent, 'teal');
});
