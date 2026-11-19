// xterm palettes (DR(g)): Radix dark steps 10/11, light step 11. Background/foreground/cursor are resolved from
// the live tokens so the terminal is visually the panel (opaque --surface-0).
import type { ITheme } from '@xterm/xterm';

export const xtermDark: ITheme = {
  background: '#111113', foreground: '#edeef0', cursor: '#9b9ef0', cursorAccent: '#111113',
  selectionBackground: 'rgba(91,91,214,0.35)', selectionInactiveBackground: 'rgba(255,255,255,0.10)',
  scrollbarSliderBackground: 'rgba(255,255,255,0.10)', scrollbarSliderHoverBackground: 'rgba(255,255,255,0.18)', scrollbarSliderActiveBackground: 'rgba(255,255,255,0.24)',
  black: '#212225', red: '#ec5d5e', green: '#33b074', yellow: '#ffc53d', blue: '#3b9eff', magenta: '#b658c4', cyan: '#23afd0', white: '#b0b4ba',
  brightBlack: '#696e77', brightRed: '#ff9592', brightGreen: '#3dd68c', brightYellow: '#ffca16', brightBlue: '#70b8ff', brightMagenta: '#e796f3', brightCyan: '#4ccce6', brightWhite: '#edeef0',
};
export const xtermLight: ITheme = {
  background: '#fcfcfd', foreground: '#1c2024', cursor: '#5b5bd6', cursorAccent: '#fcfcfd',
  selectionBackground: 'rgba(91,91,214,0.22)', selectionInactiveBackground: 'rgba(0,0,45,0.08)',
  scrollbarSliderBackground: 'rgba(0,0,45,0.10)', scrollbarSliderHoverBackground: 'rgba(0,0,45,0.18)', scrollbarSliderActiveBackground: 'rgba(0,0,45,0.24)',
  black: '#1c2024', red: '#ce2c31', green: '#218358', yellow: '#ab6400', blue: '#0d74ce', magenta: '#6550b9', cyan: '#008573', white: '#60646c',
  brightBlack: '#80838d', brightRed: '#dc3e42', brightGreen: '#2b9a66', brightYellow: '#ab6400', brightBlue: '#0588f0', brightMagenta: '#654dc4', brightCyan: '#0d9b8a', brightWhite: '#8b8d98',
};

/** Only accept plain colour strings from CSS (xterm needs real colours, not var()). */
function color(v: string, fallback: string): string {
  const s = v.trim();
  return /^(#[0-9a-f]{3,8}|rgba?\([^()]*\)|hsla?\([^()]*\)|oklch\([^()]*\)|color\([^()]*\))$/i.test(s) ? s : fallback;
}

/** Palette for the current document theme with token-resolved base colours. */
export function resolveXtermTheme(doc: Document = document): ITheme {
  const light = doc.documentElement.dataset.theme === 'light';
  const base = light ? xtermLight : xtermDark;
  let cs: CSSStyleDeclaration | null = null;
  try { cs = getComputedStyle(doc.documentElement); } catch { /* no layout */ }
  const v = (n: string) => (cs ? cs.getPropertyValue(n) : '');
  const bg = color(v('--surface-0'), base.background!);
  return {
    ...base,
    background: bg,
    cursorAccent: bg,
    foreground: color(v('--text-1'), base.foreground!),
    cursor: color(v('--accent-11'), base.cursor!),
  };
}

/** Search decoration colours (must be #RRGGBB). */
export function searchDecorations(light: boolean) {
  return light
    ? { matchBackground: '#e6e7fb', matchOverviewRuler: '#8e8ee6', activeMatchBackground: '#c6c8f6', activeMatchColorOverviewRuler: '#5b5bd6' }
    : { matchBackground: '#2a2a55', matchOverviewRuler: '#5b5bd6', activeMatchBackground: '#4747a6', activeMatchColorOverviewRuler: '#9b9ef0' };
}
