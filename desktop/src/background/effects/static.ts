// B0 solid, B1 mesh gradient (+ optional B2 dots), B2 dot/line grid.
// Two outputs from the same description: CSS layers (the live background, zero
// canvas cost) and a Canvas2D drawing (thumbnails; works in a worker).
import type { BgConfig } from '../../shared/bg';
import { type Palette, toCss, mix, effectiveIntensity } from '../palette';
import { mulberry32, deriveSeed } from '../prng';

export interface Bloom { x: number; y: number; rx: number; ry: number; color: [number, number, number]; alpha: number }
export interface StaticSpec {
  base: string;
  blooms: Bloom[];                 // x,y,rx,ry in 0..1 of the viewport
  vignette: number;                // 0..1 darkening at the edges
  grid: null | { style: 'dots' | 'lines'; size: number; color: string };
  drift: boolean;
}

export function staticSpec(cfg: BgConfig, pal: Palette): StaticSpec {
  const k = effectiveIntensity(pal, cfg.intensity);
  const base = toCss(pal.c0);
  if (cfg.id === 'solid') return { base, blooms: [], vignette: 0, grid: null, drift: false };
  const gridColor = pal.dark ? `rgba(255, 255, 255, ${(0.05 + 0.1 * k).toFixed(3)})` : `rgba(0, 0, 0, ${(0.05 + 0.1 * k).toFixed(3)})`;
  if (cfg.id === 'grid') {
    return {
      base, blooms: [], vignette: pal.dark ? 0.25 * k : 0, drift: false,
      grid: { style: cfg.params.style === 'lines' ? 'lines' : 'dots', size: Number(cfg.params.size) || 20, color: gridColor },
    };
  }
  // mesh
  const rng = mulberry32(deriveSeed(cfg.seed, 'mesh'));
  const bloom = Number(cfg.params.bloom ?? 0.6);
  const corners: [number, number][] = [[0.12, 0.08], [0.9, 0.14], [0.78, 0.92], [0.08, 0.85]];
  const start = Math.floor(rng() * 4);
  const blooms: Bloom[] = [];
  for (let i = 0; i < 3; i++) {
    const [cx, cy] = corners[(start + i) % 4];
    blooms.push({
      x: cx + (rng() - 0.5) * 0.2, y: cy + (rng() - 0.5) * 0.2,
      rx: 0.45 + rng() * 0.25, ry: 0.4 + rng() * 0.25,
      color: i === 1 ? pal.c2 : i === 2 ? mix(pal.c2, pal.c3, 0.3) : mix(pal.c1, pal.c2, 0.6),
      alpha: Math.min(1, (0.35 + 0.65 * bloom) * (0.5 + k)) * (i === 1 ? 0.55 : 0.8),
    });
  }
  return {
    base, blooms, vignette: pal.dark ? 0.3 : 0.06, drift: cfg.params.drift === true,
    grid: cfg.params.grid === false ? null : { style: 'dots', size: 20, color: gridColor },
  };
}

const pct = (v: number) => `${(v * 100).toFixed(1)}%`;

/** CSS background layers, bottom first. Applied by the engine to its own divs. */
export function staticCss(spec: StaticSpec): { blooms: string; vignette: string; grid: string; gridSize: string; gridMask: string; base: string } {
  const blooms = spec.blooms.map((b) =>
    `radial-gradient(ellipse ${pct(b.rx)} ${pct(b.ry)} at ${pct(b.x)} ${pct(b.y)}, ${toCss(b.color, b.alpha)} 0%, ${toCss(b.color, 0)} 100%)`).join(', ');
  const vignette = spec.vignette > 0 ? `radial-gradient(ellipse at 50% 40%, rgba(0,0,0,0) 40%, rgba(0,0,0,${spec.vignette.toFixed(3)}) 100%)` : 'none';
  let grid = 'none', gridSize = 'auto';
  if (spec.grid) {
    const s = spec.grid.size;
    gridSize = `${s}px ${s}px`;
    grid = spec.grid.style === 'dots'
      ? `radial-gradient(circle, ${spec.grid.color} 1px, transparent 1.5px)`
      : `linear-gradient(${spec.grid.color} 1px, transparent 1px), linear-gradient(90deg, ${spec.grid.color} 1px, transparent 1px)`;
  }
  return { base: spec.base, blooms: blooms || 'none', vignette, grid, gridSize,
    gridMask: 'radial-gradient(ellipse at 50% 30%, #000 30%, transparent 75%)' };
}

type Ctx2D = CanvasRenderingContext2D | OffscreenCanvasRenderingContext2D;

/** Same look drawn into a 2D context (thumbnails, CSS-less fallback). */
export function drawStatic2d(ctx: Ctx2D, w: number, h: number, spec: StaticSpec): void {
  ctx.save();
  ctx.globalCompositeOperation = 'source-over';
  ctx.fillStyle = spec.base; ctx.fillRect(0, 0, w, h);
  for (const b of spec.blooms) {
    ctx.save();
    ctx.translate(b.x * w, b.y * h);
    ctx.scale(b.rx * w, b.ry * h);
    const g = ctx.createRadialGradient(0, 0, 0, 0, 0, 1);
    g.addColorStop(0, toCss(b.color, b.alpha)); g.addColorStop(1, toCss(b.color, 0));
    ctx.fillStyle = g; ctx.fillRect(-1, -1, 2, 2);
    ctx.restore();
  }
  if (spec.grid) {
    const s = Math.max(4, spec.grid.size * Math.max(0.25, Math.min(1, w / 1200)));
    ctx.fillStyle = spec.grid.color;
    for (let y = s / 2; y < h; y += s) {
      for (let x = s / 2; x < w; x += s) {
        const dx = (x / w - 0.5) / 0.75, dy = (y / h - 0.3) / 0.75;
        const fade = Math.max(0, 1 - Math.hypot(dx, dy));
        if (fade <= 0) continue;
        ctx.globalAlpha = Math.min(1, fade * 1.6);
        if (spec.grid.style === 'dots') ctx.fillRect(x - 0.5, y - 0.5, 1, 1);
        else { ctx.fillRect(x - s / 2, y, s, 0.5); ctx.fillRect(x, y - s / 2, 0.5, s); }
      }
    }
    ctx.globalAlpha = 1;
  }
  if (spec.vignette > 0) {
    const r = Math.hypot(w, h) / 2;
    const g = ctx.createRadialGradient(w / 2, h * 0.4, r * 0.4, w / 2, h * 0.4, r);
    g.addColorStop(0, 'rgba(0,0,0,0)'); g.addColorStop(1, `rgba(0,0,0,${spec.vignette})`);
    ctx.fillStyle = g; ctx.fillRect(0, 0, w, h);
  }
  ctx.restore();
}
