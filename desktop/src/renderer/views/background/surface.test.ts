import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { surfaceAlphas, worstPixels, contrast, over, minAlpha, AA } from './surface';
import { parseColor, type RGB } from '../../../background/palette';
import type { BgTheme } from '../../../shared/bg';

// Read the real tokens so a palette change re-runs the contrast proof.
const css = readFileSync(join(process.cwd(), 'src', 'renderer', 'styles', 'tokens.css'), 'utf8'); // run-tests.mjs runs from desktop/
const block = (sel: string) => { const i = css.indexOf(sel); return css.slice(i, css.indexOf('\n}', i)); };
const tok = (b: string, name: string) => { const m = new RegExp(`${name}:\\s*(#[0-9a-fA-F]{6})`).exec(b); assert.ok(m, name); return m![1]; };
const THEMES = { dark: block(':root, [data-theme="dark"]'), light: block('[data-theme="light"] {') };
const ACCENTS = ['iris', 'violet', 'blue', 'teal', 'amber', 'crimson'];
const accent = (mode: 'dark' | 'light', a: string) => {
  const line = css.split('\n').find((l) => l.startsWith(mode === 'dark' ? `[data-accent="${a}"]` : `[data-theme="light"][data-accent="${a}"]`))!;
  return { a9: tok(line, '--accent-9'), a11: tok(line, '--accent-11') };
};
const theme = (mode: 'dark' | 'light', a: string): BgTheme => {
  const b = THEMES[mode], ac = accent(mode, a);
  return { mode, base: tok(b, '--bg-window'), fg: tok(b, '--text-1'), accent: ac.a9, accent2: ac.a11 };
};
const texts = (mode: 'dark' | 'light') => ['--text-1', '--text-2', '--text-3'].map((n) => tok(THEMES[mode], n));

test('surface: every theme x accent x intensity x seeThrough keeps text-1/2/3 >= 4.5:1 over the worst pixel', () => {
  let checked = 0;
  for (const mode of ['dark', 'light'] as const) {
    const surface = tok(THEMES[mode], '--surface-0');
    for (const a of ACCENTS) for (const s of [0, 0.25, 0.5, 0.75, 1]) {
      const th = theme(mode, a);
      const out = surfaceAlphas({ seeThrough: s, theme: th, surface, texts: texts(mode) });
      for (const alpha of [out.panel, out.chrome]) for (const px of worstPixels(th)) for (const t of texts(mode)) {
        const c = contrast(parseColor(t)!, over(parseColor(surface)!, alpha, px));
        assert.ok(c >= AA - 1e-6, `${mode}/${a} s=${s} alpha=${alpha} ${t}: ${c.toFixed(2)}`);
        checked++;
      }
    }
  }
  assert.ok(checked > 700);
});

test('surface: the default look is actually see-through (dark and light, every accent)', () => {
  for (const mode of ['dark', 'light'] as const) for (const a of ACCENTS) {
    const out = surfaceAlphas({ seeThrough: 0.5, theme: theme(mode, a), surface: tok(THEMES[mode], '--surface-0'), texts: texts(mode) });
    assert.ok(out.panel < 0.9, `${mode}/${a} panel ${out.panel}`);
    assert.ok(out.chrome <= out.panel, `${mode}/${a} chrome ${out.chrome} <= panel ${out.panel}`);
  }
});

test('surface: seeThrough 0 is opaque; more see-through never raises alpha', () => {
  const th = theme('dark', 'iris'), surface = tok(THEMES.dark, '--surface-0');
  const at = (s: number) => surfaceAlphas({ seeThrough: s, theme: th, surface, texts: texts('dark') });
  assert.deepEqual([at(0).panel, at(0).chrome], [1, 1]);
  let prev = at(0);
  for (let s = 0.1; s <= 1.0001; s += 0.1) { const o = at(s); assert.ok(o.panel <= prev.panel && o.chrome <= prev.chrome); prev = o; }
});

test('surface: junk input falls back to opaque or sane values', () => {
  const th = theme('dark', 'iris');
  assert.deepEqual(surfaceAlphas({ seeThrough: 0.5, theme: th, surface: 'nope', texts: ['#fff'] }), { panel: 1, chrome: 1, floor: 1 });
  assert.deepEqual(surfaceAlphas({ seeThrough: 0.5, theme: th, surface: '#111113', texts: [] }), { panel: 1, chrome: 1, floor: 1 });
  const o = surfaceAlphas({ seeThrough: NaN, theme: th, surface: '#111113', texts: ['#edeef0'] });
  assert.ok(o.panel > 0 && o.panel <= 1 && o.chrome > 0 && o.chrome <= 1);
  // A text colour that fails even on the opaque surface forces opaque.
  assert.equal(minAlpha(parseColor('#111113') as RGB, [parseColor('#222222') as RGB], [[0.07, 0.07, 0.07]]), 1);
});

test('surface: floors per theme/accent (informational)', () => {
  for (const mode of ['dark', 'light'] as const) console.log(mode, ACCENTS.map((a) => `${a}:${surfaceAlphas({ seeThrough: 0.5, theme: theme(mode, a), surface: tok(THEMES[mode], '--surface-0'), texts: texts(mode) }).floor}`).join(' '));
});
