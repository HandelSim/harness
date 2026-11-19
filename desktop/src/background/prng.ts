// Seeded PRNG (mulberry32). All background randomness comes from here so that
// the same seed + params give the same sequence.
export type Rng = () => number;   // [0, 1)

export function mulberry32(seed: number): Rng {
  let a = (seed >>> 0) || 0x9e3779b9;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Derive an independent stream from a seed and a label (e.g. per-effect). */
export function deriveSeed(seed: number, label: string): number {
  let h = (seed >>> 0) ^ 0x811c9dc5;
  for (let i = 0; i < label.length; i++) {
    h ^= label.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h >>> 0;
}

export function range(rng: Rng, lo: number, hi: number): number { return lo + (hi - lo) * rng(); }
export function int(rng: Rng, lo: number, hiExclusive: number): number { return lo + Math.floor(rng() * (hiExclusive - lo)); }
