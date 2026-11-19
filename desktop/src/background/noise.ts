// Seeded noise. simplex-noise 4.0.3 for 3D simplex (flow field), plus a cheap
// hashed value noise + fBm (silk CPU fallback; same family as the GLSL version).
import { createNoise3D } from 'simplex-noise';
import { mulberry32, type Rng } from './prng';

export type Noise3 = (x: number, y: number, z: number) => number;   // [-1, 1]

export function simplex3(seed: number): Noise3 { return createNoise3D(mulberry32(seed)); }

/** Curl of a scalar potential psi(x, y, t): v = (dpsi/dy, -dpsi/dx), central differences. Divergence-free. */
export function curl2(n: Noise3, x: number, y: number, t: number, eps = 1e-3): [number, number] {
  const dy = (n(x, y + eps, t) - n(x, y - eps, t)) / (2 * eps);
  const dx = (n(x + eps, y, t) - n(x - eps, y, t)) / (2 * eps);
  return [dy, -dx];
}

/** 2D value noise in [0, 1], C1-continuous (smoothstep interpolation), seeded via a permutation table. */
export function valueNoise2(seed: number | Rng): (x: number, y: number) => number {
  const rng = typeof seed === 'function' ? seed : mulberry32(seed);
  const perm = new Uint8Array(512), vals = new Float32Array(256);
  const p = Array.from({ length: 256 }, (_, i) => i);
  for (let i = 255; i > 0; i--) { const j = Math.floor(rng() * (i + 1)); [p[i], p[j]] = [p[j], p[i]]; }
  for (let i = 0; i < 512; i++) perm[i] = p[i & 255];
  for (let i = 0; i < 256; i++) vals[i] = rng();
  return (x: number, y: number) => {
    const xi = Math.floor(x), yi = Math.floor(y);
    const xf = x - xi, yf = y - yi;
    const X = xi & 255, Y = yi & 255;
    const u = xf * xf * (3 - 2 * xf), v = yf * yf * (3 - 2 * yf);
    const a = vals[perm[X + perm[Y]]], b = vals[perm[X + 1 + perm[Y]]];
    const c = vals[perm[X + perm[Y + 1]]], d = vals[perm[X + 1 + perm[Y + 1]]];
    return a + (b - a) * u + (c - a) * v + (a - b - c + d) * u * v;
  };
}

/** fBm of a [0,1] noise: 5 octaves, lacunarity 2, gain 0.5, normalised back to [0, 1]. */
export function fbm2(n: (x: number, y: number) => number, x: number, y: number, octaves = 5): number {
  let s = 0, a = 0.5, norm = 0;
  for (let o = 0; o < octaves; o++) { s += a * n(x, y); norm += a; x = x * 2 + 17.3; y = y * 2 + 9.1; a *= 0.5; }
  return s / norm;
}
