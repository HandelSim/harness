// B6 Clifford / de Jong density (2D maps) and the pure math for B7 (3D ODEs + RK4).
import type { Effect, Ctx2D, EffectInit } from './types';
import { fitCanvas } from './types';
import { type Palette, ramp } from '../palette';
import { mulberry32, deriveSeed } from '../prng';
import { CLIFFORD_PRESETS, DEJONG_PRESETS } from '../catalog';

export type Map2 = 'clifford' | 'dejong';
export function mapStep(kind: Map2, p: readonly number[], x: number, y: number): [number, number] {
  const [a, b, c, d] = p;
  return kind === 'clifford'
    ? [Math.sin(a * y) + c * Math.cos(a * x), Math.sin(b * x) + d * Math.cos(b * y)]
    : [Math.sin(a * y) - Math.cos(b * x), Math.sin(c * x) - Math.cos(d * y)];
}
/** Analytic bound of |x|, |y| for the map (used to fit the image). */
export function mapBounds(kind: Map2, p: readonly number[]): [number, number] {
  return kind === 'clifford' ? [1 + Math.abs(p[2]), 1 + Math.abs(p[3])] : [2, 2];
}

// ---------------------------------------------------------------- 3D systems (B7)
export type Vec3 = [number, number, number];
export type Ode = (s: Vec3) => Vec3;
export const SYSTEMS: Record<string, { f: Ode; dt: number; start: Vec3 }> = {
  lorenz: { dt: 0.006, start: [0.1, 0, 0], f: ([x, y, z]) => [10 * (y - x), x * (28 - z) - y, x * y - (8 / 3) * z] },
  aizawa: { dt: 0.01, start: [0.1, 0, 0], f: ([x, y, z]) => {
    const a = 0.95, b = 0.7, c = 0.6, d = 3.5, e = 0.25, f = 0.1;
    return [(z - b) * x - d * y, d * x + (z - b) * y, c + a * z - z ** 3 / 3 - (x * x + y * y) * (1 + e * z) + f * z * x ** 3];
  } },
  thomas: { dt: 0.05, start: [0.1, 0, 0], f: ([x, y, z]) => {
    const b = 0.208186;
    return [Math.sin(y) - b * x, Math.sin(z) - b * y, Math.sin(x) - b * z];
  } },
  halvorsen: { dt: 0.006, start: [-1.48, -1.51, 2.04], f: ([x, y, z]) => {
    const a = 1.89;
    return [-a * x - 4 * y - 4 * z - y * y, -a * y - 4 * z - 4 * x - z * z, -a * z - 4 * x - 4 * y - x * x];
  } },
};

export function rk4(f: Ode, s: Vec3, h: number): Vec3 {
  const k1 = f(s);
  const k2 = f([s[0] + h / 2 * k1[0], s[1] + h / 2 * k1[1], s[2] + h / 2 * k1[2]]);
  const k3 = f([s[0] + h / 2 * k2[0], s[1] + h / 2 * k2[1], s[2] + h / 2 * k2[2]]);
  const k4 = f([s[0] + h * k3[0], s[1] + h * k3[1], s[2] + h * k3[2]]);
  return [
    s[0] + h / 6 * (k1[0] + 2 * k2[0] + 2 * k3[0] + k4[0]),
    s[1] + h / 6 * (k1[1] + 2 * k2[1] + 2 * k3[1] + k4[1]),
    s[2] + h / 6 * (k1[2] + 2 * k2[2] + 2 * k3[2] + k4[2]),
  ];
}

// ---------------------------------------------------------------- B6 effect
const BUILD_TOTAL = 4_000_000;     // iterations until a non-morphing image is "done"

/** True when the orbit for p fills an area (chaotic), false when it has collapsed onto a
 *  fixed point or short cycle. Chaotic maps have periodic windows densely packed in
 *  parameter space, so a morph must check its path, not just its endpoints. */
export function spreadOk(kind: Map2, p: readonly number[]): boolean {
  let x = 0.1, y = 0.1;
  const seen = new Set<number>();
  for (let i = 0; i < 1500; i++) {
    const r = mapStep(kind, p, x, y); x = r[0]; y = r[1];
    if (i > 100) seen.add(((x * 20 + 200) | 0) * 1000 + ((y * 20 + 200) | 0));
  }
  return seen.size > 120;
}

/** Next morph waypoint near base whose whole straight path from `from` stays chaotic
 *  (checked at 64 points). Falls back to base (the path back was already walked). */
export function pickWaypoint(kind: Map2, base: readonly number[], from: readonly number[], rng: () => number, reach = 0.15): number[] {
  for (let tries = 0; tries < 16; tries++) {
    const cand = base.map((v) => v + (rng() * 2 - 1) * reach);
    let ok = true;
    for (let i = 1; i <= 64 && ok; i++) ok = spreadOk(kind, from.map((v, j) => v + (cand[j] - v) * (i / 64)));
    if (ok) return cand;
  }
  return base.slice();
}

export function makeAttractor2d(ctx: Ctx2D, init: EffectInit): Effect {
  const kind: Map2 = init.cfg.params.map === 'dejong' ? 'dejong' : 'clifford';
  const presets = kind === 'clifford' ? CLIFFORD_PRESETS : DEJONG_PRESETS;
  const pi = Math.min(presets.length - 1, Math.max(0, Number(init.cfg.params.preset) || 0)) % presets.length;
  const morph = init.cfg.params.morph !== false;
  const rng = mulberry32(deriveSeed(init.cfg.seed, 'attractor'));
  let pal: Palette = init.pal, k = 0.5;
  let W = 1, H = 1, hist = new Float32Array(1), img: ImageData | null = null;
  let x = rng() * 0.2, y = rng() * 0.2, total = 0, dirty = true;
  let lut = new Uint8ClampedArray(256 * 3);
  const base = presets[pi];
  const p = base.slice();
  let from = base.slice(), to = morph ? pickWaypoint(kind, base, base, rng) : base.slice(), leg = 0, decay = 1;
  const LEG_S = 20;
  let sx = 1, sy = 1, ox = 0, oy = 0;

  const refit = () => {
    const reach = morph ? 0.15 : 0;
    const big = base.map((v) => Math.abs(v) + reach);
    const [bx, by] = mapBounds(kind, big);
    const s = Math.min(W / (2 * bx), H / (2 * by)) * 0.92;
    sx = s; sy = s; ox = W / 2; oy = H / 2;
  };
  const buildLut = () => {
    lut = new Uint8ClampedArray(256 * 3);
    for (let i = 0; i < 256; i++) {
      const v = i / 255;
      const c = ramp(pal, Math.min(1, v * (0.55 + 0.45 * k) + 0.0));
      lut[i * 3] = c[0] * 255; lut[i * 3 + 1] = c[1] * 255; lut[i * 3 + 2] = c[2] * 255;
    }
    dirty = true;
  };
  const iterate = (n: number) => {
    for (let i = 0; i < n; i++) {
      const r = mapStep(kind, p, x, y); x = r[0]; y = r[1];
      const px = (x * sx + ox) | 0, py = (y * sy + oy) | 0;
      if (px >= 0 && py >= 0 && px < W && py < H) hist[py * W + px] += 1;
    }
    total += n; dirty = true;
  };
  return {
    resize(w, h, cap) {
      if (fitCanvas(init.canvas, w, h, Math.min(0.5, cap)) || hist.length !== init.canvas.width * init.canvas.height) {
        W = init.canvas.width; H = init.canvas.height;
        hist = new Float32Array(W * H); img = ctx.createImageData(W, H); total = 0; refit(); dirty = true;
      }
    },
    setPalette(np, intensity) { pal = np; k = intensity; buildLut(); },
    step(dt) {
      if (morph) {
        leg += dt / LEG_S;
        if (leg >= 1) { leg = 0; from = to; to = pickWaypoint(kind, base, from, rng); }
        const m = 0.5 - 0.5 * Math.cos(leg * Math.PI);                  // eased leg between verified waypoints
        for (let i = 0; i < 4; i++) p[i] = from[i] + (to[i] - from[i]) * m;
        // Periodic windows are too dense to route around entirely: while the orbit has
        // collapsed, hold the current image (no decay, no new points) until it is chaotic again.
        if (!spreadOk(kind, p)) return;
        decay *= 0.985;                                                  // applied once per draw, not per step
        iterate(Math.round(20_000 * (W * H) / 300_000) + 6000);
      } else if (total < BUILD_TOTAL) iterate(60_000);
    },
    draw() {
      if (!dirty || !img) return;
      dirty = false;
      if (decay < 1) { for (let i = 0; i < hist.length; i++) hist[i] *= decay; decay = 1; }
      let max = 1;
      for (let i = 0; i < hist.length; i++) if (hist[i] > max) max = hist[i];
      const inv = 1 / Math.log(1 + max), d = img.data;
      for (let i = 0, j = 0; i < hist.length; i++, j += 4) {
        const v = hist[i] > 0 ? Math.log(1 + hist[i]) * inv : 0;
        const li = Math.min(255, (Math.sqrt(v) * 255) | 0) * 3;
        d[j] = lut[li]; d[j + 1] = lut[li + 1]; d[j + 2] = lut[li + 2]; d[j + 3] = 255;
      }
      ctx.putImageData(img, 0, 0);
    },
    settle() { if (total < 1_500_000) iterate(1_500_000 - total); },
    dispose() { hist = new Float32Array(1); img = null; },
  };
}
