// Theme-aware palette. Pure (no DOM): parses resolved CSS colours and derives a
// low-saturation 4-stop ramp so the UI on top stays readable.
import type { BgTheme } from '../shared/bg';

export type RGB = [number, number, number];          // 0..1 each
export interface Palette { c0: RGB; c1: RGB; c2: RGB; c3: RGB; dark: boolean; maxIntensity: number }

const clamp01 = (x: number) => (x < 0 ? 0 : x > 1 ? 1 : x);

/** Parse #rgb, #rgba, #rrggbb, #rrggbbaa, rgb()/rgba() (comma or space syntax), hsl()/hsla(). Alpha is dropped. */
export function parseColor(input: string): RGB | null {
  const s = String(input ?? '').trim().toLowerCase();
  if (!s) return null;
  let m = /^#([0-9a-f]{3,8})$/.exec(s);
  if (m) {
    const h = m[1];
    if (h.length === 3 || h.length === 4) {
      return [0, 1, 2].map((i) => parseInt(h[i] + h[i], 16) / 255) as RGB;
    }
    if (h.length === 6 || h.length === 8) {
      return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) / 255) as RGB;
    }
    return null;
  }
  m = /^rgba?\(\s*([^)]*)\)$/.exec(s);
  if (m) {
    const parts = m[1].split(/[\s,/]+/).filter(Boolean).slice(0, 3);
    if (parts.length < 3) return null;
    const v = parts.map((p) => (p.endsWith('%') ? parseFloat(p) / 100 : parseFloat(p) / 255));
    if (v.some((x) => !Number.isFinite(x))) return null;
    return v.map(clamp01) as RGB;
  }
  m = /^hsla?\(\s*([^)]*)\)$/.exec(s);
  if (m) {
    const parts = m[1].split(/[\s,/]+/).filter(Boolean).slice(0, 3);
    if (parts.length < 3) return null;
    const h = (((parseFloat(parts[0]) % 360) + 360) % 360) / 360;
    const sat = parseFloat(parts[1]) / 100, l = parseFloat(parts[2]) / 100;
    if (![h, sat, l].every(Number.isFinite)) return null;
    return hslToRgb(h, clamp01(sat), clamp01(l));
  }
  return null;
}

function hslToRgb(h: number, s: number, l: number): RGB {
  if (s === 0) return [l, l, l];
  const q = l < 0.5 ? l * (1 + s) : l + s - l * s, p = 2 * l - q;
  const f = (t: number) => {
    if (t < 0) t += 1; if (t > 1) t -= 1;
    if (t < 1 / 6) return p + (q - p) * 6 * t;
    if (t < 1 / 2) return q;
    if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
    return p;
  };
  return [f(h + 1 / 3), f(h), f(h - 1 / 3)];
}

export function mix(a: RGB, b: RGB, t: number): RGB {
  return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
}
export function luminance(c: RGB): number {
  const f = (x: number) => (x <= 0.04045 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4);
  return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]);
}
export function desaturate(c: RGB, amount: number): RGB {
  const y = 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2];
  return mix(c, [y, y, y], amount);
}
export function toCss(c: RGB, alpha = 1): string {
  const r = Math.round(clamp01(c[0]) * 255), g = Math.round(clamp01(c[1]) * 255), b = Math.round(clamp01(c[2]) * 255);
  return alpha >= 1 ? `rgb(${r}, ${g}, ${b})` : `rgba(${r}, ${g}, ${b}, ${+alpha.toFixed(3)})`;
}

export const DEFAULT_THEME: BgTheme = { mode: 'dark', base: '#0b0c0e', fg: '#b0b4ba', accent: '#5b5bd6', accent2: '#b1a9ff' };

/**
 * Derive the ramp. c0 = base; c1 = a dim accent (≈accent-3); c2 = a mid accent (≈accent-8, desaturated);
 * c3 = a faint highlight toward fg. Dark: highlights stay low so mean luminance stays near the base.
 * Light: the ramp is inverted (accents darker than base) and intensity is capped at 0.35.
 */
export function derivePalette(theme: BgTheme | null | undefined): Palette {
  const t = theme ?? DEFAULT_THEME;
  const dark = t.mode !== 'light';
  const base = parseColor(t.base) ?? parseColor(dark ? DEFAULT_THEME.base : '#eceef2')!;
  const fg = parseColor(t.fg) ?? (dark ? parseColor(DEFAULT_THEME.fg)! : ([0.1, 0.1, 0.12] as RGB));
  const a1 = desaturate(parseColor(t.accent) ?? parseColor(DEFAULT_THEME.accent)!, 0.25);
  const a2 = desaturate(parseColor(t.accent2) ?? parseColor(t.accent) ?? parseColor(DEFAULT_THEME.accent2)!, 0.3);
  if (dark) {
    return {
      c0: base,
      c1: mix(base, a1, 0.28),
      c2: mix(base, a2, 0.55),
      c3: mix(base, fg, 0.42),
      dark, maxIntensity: 1,
    };
  }
  return {
    c0: base,
    c1: mix(base, a1, 0.22),
    c2: mix(base, a1, 0.5),
    c3: mix(base, mix(a1, fg, 0.5), 0.6),
    dark, maxIntensity: 0.35,
  };
}

/** Colour at position x in [0,1] along c0 -> c1 -> c2 -> c3. */
export function ramp(p: Palette, x: number): RGB {
  const v = clamp01(x) * 3;
  if (v < 1) return mix(p.c0, p.c1, v);
  if (v < 2) return mix(p.c1, p.c2, v - 1);
  return mix(p.c2, p.c3, v - 2);
}

/** Effective field strength: user intensity times the theme cap. */
export function effectiveIntensity(p: Palette, intensity: number): number {
  return clamp01(intensity) * p.maxIntensity;
}

/** 256-entry RGBA lookup table (Uint8) for histogram / grid tone mapping; entry 0 is transparent. */
export function rampLut(p: Palette, alphaScale = 1): Uint8ClampedArray {
  const lut = new Uint8ClampedArray(256 * 4);
  for (let i = 0; i < 256; i++) {
    const c = ramp(p, i / 255);
    lut[i * 4] = c[0] * 255; lut[i * 4 + 1] = c[1] * 255; lut[i * 4 + 2] = c[2] * 255;
    lut[i * 4 + 3] = Math.min(255, i * 4 * alphaScale);
  }
  return lut;
}
