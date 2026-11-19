// Surface opacity policy (W2-G), pure. The app's chrome and dock groups are translucent so the background shows
// through; appearance.seeThrough (0 = opaque .. 1 = most see-through) picks how much, and a contrast floor keeps
// every text colour >= WCAG AA (4.5:1) over the brightest/darkest pixel the background can draw at this intensity.
import { derivePalette, mix, luminance, parseColor, type RGB } from '../../../background/palette';
import type { BgTheme } from '../../../shared/bg';

export const AA = 4.5;
/** How far each kind of surface may go toward clear at seeThrough = 1 (before the contrast floor). */
const RANGE = { panel: 0.6, chrome: 0.72 };

export function contrast(a: RGB, b: RGB): number {
  const x = luminance(a), y = luminance(b);
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
}
/** sRGB "source over" of fg at alpha a on an opaque bg (what Chromium composites). */
export function over(fg: RGB, a: number, bg: RGB): RGB { return mix(bg, fg, a); }

/** The extreme pixels the engine can draw. Intensity only scales each effect's gain (density, trail deposit), so a
 *  dense spot reaches the end of the ramp at any intensity: every ramp stop is a possible pixel (measured: flow at
 *  intensity 0.6 drew c3-bright strands). */
export function worstPixels(theme: BgTheme): RGB[] {
  const p = derivePalette(theme);
  return [p.c0, p.c1, p.c2, p.c3];
}

/** Smallest alpha of `surface` over every pixel that keeps every text colour >= ratio (1 if even opaque fails). */
export function minAlpha(surface: RGB, texts: RGB[], pixels: RGB[], ratio = AA): number {
  let need = 0;
  for (const px of pixels) {
    for (const t of texts) {
      if (contrast(t, over(surface, need, px)) >= ratio) continue;
      if (contrast(t, surface) < ratio) return 1;
      let lo = need, hi = 1;
      for (let i = 0; i < 24; i++) { const m = (lo + hi) / 2; if (contrast(t, over(surface, m, px)) >= ratio) hi = m; else lo = m; }
      need = hi;
    }
  }
  return need;
}

export interface SurfaceIn { seeThrough: number; theme: BgTheme; surface: string; texts: string[] }
export interface SurfaceOut { panel: number; chrome: number; floor: number }

export function surfaceAlphas(i: SurfaceIn): SurfaceOut {
  const s = Number.isFinite(i.seeThrough) ? Math.min(1, Math.max(0, i.seeThrough)) : 0.5;
  const surf = parseColor(i.surface);
  const texts = i.texts.map(parseColor).filter((c): c is RGB => !!c);
  if (!surf || !texts.length) return { panel: 1, chrome: 1, floor: 1 };
  const floor = minAlpha(surf, texts, worstPixels(i.theme));
  const fl = Math.ceil(floor * 1000) / 1000;   // round the floor up so rounding never breaks the guarantee
  const r = (x: number) => Math.min(1, Math.max(fl, Math.round(x * 1000) / 1000));
  return { panel: r(1 - s * RANGE.panel), chrome: r(1 - s * RANGE.chrome), floor: fl };
}
