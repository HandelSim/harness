// B9: Conway's Life (B3/S23) on a coarse wrapping grid, displayed with fading trails.
import type { Effect, Ctx2D, EffectInit } from './types';
import { type Palette, ramp } from '../palette';
import { mulberry32, deriveSeed, type Rng } from '../prng';

/** One generation, toroidal wrap. Returns the live count. */
export function lifeStep(cur: Uint8Array, next: Uint8Array, w: number, h: number): number {
  let live = 0;
  for (let y = 0; y < h; y++) {
    const ym = ((y - 1 + h) % h) * w, y0 = y * w, yp = ((y + 1) % h) * w;
    for (let x = 0; x < w; x++) {
      const xm = (x - 1 + w) % w, xp = (x + 1) % w;
      const n = cur[ym + xm] + cur[ym + x] + cur[ym + xp] + cur[y0 + xm] + cur[y0 + xp] + cur[yp + xm] + cur[yp + x] + cur[yp + xp];
      const v = n === 3 || (n === 2 && cur[y0 + x]) ? 1 : 0;
      next[y0 + x] = v; live += v;
    }
  }
  return live;
}

export const GLIDER: [number, number][] = [[1, 0], [2, 1], [0, 2], [1, 2], [2, 2]];
export const RPENTOMINO: [number, number][] = [[1, 0], [2, 0], [0, 1], [1, 1], [1, 2]];
export function stamp(g: Uint8Array, w: number, h: number, cells: [number, number][], ox: number, oy: number): void {
  for (const [x, y] of cells) g[((oy + y) % h) * w + ((ox + x) % w)] = 1;
}

export function makeLife2d(ctx: Ctx2D, init: EffectInit): Effect {
  const cell = Math.max(3, Number(init.cfg.params.cell) || 8);
  const rate = Math.max(1, Number(init.cfg.params.rate) || 8);
  const rng: Rng = mulberry32(deriveSeed(init.cfg.seed, 'life'));
  let W = 1, H = 1, a = new Uint8Array(1), b = new Uint8Array(1), trail = new Float32Array(1), img: ImageData | null = null;
  let acc = 0, pal: Palette = init.pal, k = 0.5, lut = new Uint8ClampedArray(768);
  const seedGrid = () => {
    a.fill(0);
    for (let i = 0; i < a.length; i++) a[i] = rng() < 0.12 ? 1 : 0;
  };
  const gen = () => {
    const live = lifeStep(a, b, W, H);
    [a, b] = [b, a];
    if (live < a.length * 0.02) {
      stamp(a, W, H, rng() < 0.5 ? GLIDER : RPENTOMINO, (rng() * W) | 0, (rng() * H) | 0);
      if (live < a.length * 0.005) for (let i = 0; i < 6; i++) stamp(a, W, H, RPENTOMINO, (rng() * W) | 0, (rng() * H) | 0);
    }
    for (let i = 0; i < a.length; i++) trail[i] = Math.max(trail[i] * 0.9, a[i]);
  };
  return {
    resize(w, h) {
      const nw = Math.max(8, Math.round(w / cell)), nh = Math.max(8, Math.round(h / cell));
      if (nw === W && nh === H) return;
      W = nw; H = nh;
      init.canvas.width = W; init.canvas.height = H;
      a = new Uint8Array(W * H); b = new Uint8Array(W * H); trail = new Float32Array(W * H); img = ctx.createImageData(W, H);
      seedGrid();
    },
    setPalette(np, intensity) {
      pal = np; k = intensity;
      for (let i = 0; i < 256; i++) {
        const c = ramp(pal, (i / 255) * (0.35 + 0.6 * k));
        lut[i * 3] = c[0] * 255; lut[i * 3 + 1] = c[1] * 255; lut[i * 3 + 2] = c[2] * 255;
      }
    },
    step(dt) {
      acc += dt * rate;
      let n = 0;
      while (acc >= 1 && n < 4) { gen(); acc -= 1; n++; }
      if (n === 4) acc = 0;
    },
    draw() {
      if (!img) return;
      const d = img.data;
      for (let i = 0, j = 0; i < trail.length; i++, j += 4) {
        const li = ((trail[i] * 255) | 0) * 3;
        d[j] = lut[li]; d[j + 1] = lut[li + 1]; d[j + 2] = lut[li + 2]; d[j + 3] = 255;
      }
      ctx.putImageData(img, 0, 0);
    },
    settle() { for (let i = 0; i < 40; i++) gen(); },
    dispose() {},
  };
}
