// The background catalog (DR(e) ids in comments) and config validation. Pure.
import type { BgConfig, BgDescriptor, BgParamSpec } from '../shared/bg';

const sel = (key: string, label: string, opts: [string, string][], def: string): BgParamSpec =>
  ({ key, label, type: 'select', options: opts.map(([value, l]) => ({ value, label: l })), default: def });
const rng = (key: string, label: string, min: number, max: number, step: number, def: number): BgParamSpec =>
  ({ key, label, type: 'range', min, max, step, default: def });
const tog = (key: string, label: string, def: boolean): BgParamSpec => ({ key, label, type: 'toggle', default: def });

export const CLIFFORD_PRESETS: [number, number, number, number][] = [
  [-1.4, 1.6, 1.0, 0.7], [1.6, -0.6, -1.2, 1.6], [1.7, 1.7, 0.6, 1.2], [1.5, -1.8, 1.6, 0.9],
  [-1.7, 1.3, -0.1, -1.2], [-1.7, 1.8, -1.9, -0.4], [-1.8, -2.0, -0.5, -0.9],
];
export const DEJONG_PRESETS: [number, number, number, number][] = [
  [1.641, 1.902, 0.316, 1.525], [0.970, -1.899, 1.381, -1.506], [1.4, -2.3, 2.4, -2.1], [2.01, -2.53, 1.61, -0.33],
  [-2.7, -0.09, -0.86, -2.2], [-0.827, -1.637, 1.659, -0.943], [-2.24, 0.43, -0.65, -2.43], [-2, -2, -1.2, 2],
  [-0.709, 1.638, 0.452, 1.740],
];
// Pearson classes (F, k).
export const GRAYSCOTT_PRESETS: Record<string, [number, number]> = {
  coral: [0.050, 0.063], spots: [0.026, 0.061], worms: [0.046, 0.065],
  rings: [0.030, 0.057], spirals: [0.010, 0.041], bubbles: [0.090, 0.059],
};

const presetOpts = (n: number): [string, string][] => Array.from({ length: n }, (_, i) => [String(i), `Set ${i + 1}`]);

export const CATALOG: BgDescriptor[] = [
  { id: 'solid', name: 'Solid', group: 'system', renderer: 'css', dynamic: false, cost: 1,
    blurb: 'Plain window colour. The quietest option.', params: [] },                                    // B0
  { id: 'mesh', name: 'Mesh gradient', group: 'static', renderer: 'css', dynamic: false, cost: 1,
    blurb: 'Soft accent blooms in the corners with a vignette.',                                          // B1 (+B2)
    params: [tog('grid', 'Dot grid', true), tog('drift', 'Slow drift', false), rng('bloom', 'Bloom', 0, 1, 0.05, 0.6)] },
  { id: 'grid', name: 'Dot grid', group: 'static', renderer: 'css', dynamic: false, cost: 1,
    blurb: 'Engineering-paper dots fading toward the edges.',                                             // B2
    params: [sel('style', 'Style', [['dots', 'Dots'], ['lines', 'Lines']], 'dots'), rng('size', 'Spacing', 12, 48, 2, 20)] },
  { id: 'silk', name: 'Silk', group: 'fields', renderer: 'webgl2', dynamic: true, cost: 2,
    blurb: 'Domain-warped noise folding slowly like smoke or marbled silk.',                              // B3
    params: [rng('scale', 'Zoom', 0.5, 3, 0.1, 1.4), rng('warp', 'Warp', 1, 6, 0.5, 4)] },
  { id: 'aurora', name: 'Aurora', group: 'fields', renderer: 'webgl2', dynamic: true, cost: 1,
    blurb: 'Shimmering curtains of light across the top of the window.',                                 // B4
    params: [rng('height', 'Height', 0.15, 0.5, 0.01, 0.3), rng('bands', 'Bands', 1, 3, 1, 3)] },
  { id: 'flow', name: 'Flow field', group: 'flow', renderer: 'worker2d', dynamic: true, cost: 2,
    blurb: 'Hair-thin particles tracing curl-noise streams.',                                             // B5
    params: [rng('particles', 'Particles', 500, 4000, 100, 2000), rng('zoom', 'Zoom', 0.5, 3, 0.1, 1), rng('trail', 'Trail', 0.5, 0.99, 0.01, 0.92)] },
  { id: 'attractor', name: 'Strange attractor', group: 'chaos', renderer: 'worker2d', dynamic: true, cost: 2,
    blurb: 'Clifford and de Jong maps as a luminous long-exposure density.',                              // B6
    params: [sel('map', 'Map', [['clifford', 'Clifford'], ['dejong', 'de Jong']], 'clifford'),
      sel('preset', 'Preset', presetOpts(DEJONG_PRESETS.length), '0'), tog('morph', 'Morph slowly', true)] },
  { id: 'tracers', name: 'Attractor tracers', group: 'chaos', renderer: 'worker2d', dynamic: true, cost: 2,
    blurb: 'Glowing particles orbiting a slowly turning 3D attractor.',                                   // B7
    params: [sel('system', 'System', [['lorenz', 'Lorenz'], ['aizawa', 'Aizawa'], ['thomas', 'Thomas'], ['halvorsen', 'Halvorsen']], 'lorenz'),
      rng('count', 'Tracers', 100, 1200, 50, 600)] },
  { id: 'grayscott', name: 'Reaction-diffusion', group: 'automata', renderer: 'webgl2', dynamic: true, cost: 3,
    blurb: 'Gray-Scott patterns growing like coral, worms or spots.',                                     // B8
    params: [sel('preset', 'Pattern', Object.keys(GRAYSCOTT_PRESETS).map((k) => [k, k[0].toUpperCase() + k.slice(1)]), 'coral'),
      rng('steps', 'Steps per frame', 2, 16, 1, 8)] },
  { id: 'life', name: 'Life ghosts', group: 'automata', renderer: 'worker2d', dynamic: true, cost: 1,
    blurb: 'Conway\'s Life on a coarse grid with fading trails.',                                         // B9
    params: [rng('cell', 'Cell size', 4, 14, 1, 8), rng('rate', 'Generations / s', 2, 20, 1, 8)] },
];

const BY_ID = new Map(CATALOG.map((d) => [d.id, d]));
export function descriptor(id: string): BgDescriptor | undefined { return BY_ID.get(id); }

export function defaultParams(d: BgDescriptor): Record<string, number | string | boolean> {
  const o: Record<string, number | string | boolean> = {};
  for (const p of d.params) o[p.key] = p.default;
  return o;
}

export const DEFAULT_BG: BgConfig = {
  id: 'mesh', seed: 1, params: defaultParams(BY_ID.get('mesh')!), intensity: 0.5, speed: 1, fpsCap: 30,
};

const FPS = [15, 24, 30, 60] as const;
const num = (v: unknown, lo: number, hi: number, def: number) => {
  const x = typeof v === 'number' ? v : typeof v === 'string' && v.trim() !== '' ? Number(v) : NaN;
  return Number.isFinite(x) ? Math.min(hi, Math.max(lo, x)) : def;
};

/** Coerce anything into a valid BgConfig: unknown id -> default, params clamped/filled, unknown params dropped. */
export function normalizeConfig(input: unknown): BgConfig {
  const c = (input && typeof input === 'object' ? input : {}) as Partial<BgConfig>;
  const d = (typeof c.id === 'string' && BY_ID.get(c.id)) || BY_ID.get(DEFAULT_BG.id)!;
  const src = (c.params && typeof c.params === 'object' ? c.params : {}) as Record<string, unknown>;
  const params: Record<string, number | string | boolean> = {};
  for (const p of d.params) {
    const v = src[p.key];
    if (p.type === 'toggle') params[p.key] = typeof v === 'boolean' ? v : (p.default as boolean);
    else if (p.type === 'select') params[p.key] = typeof v === 'string' && p.options!.some((o) => o.value === v) ? v : (p.default as string);
    else {
      let x = num(v, p.min!, p.max!, p.default as number);
      if (p.step) x = Math.round((x - p.min!) / p.step) * p.step + p.min!;
      params[p.key] = Math.min(p.max!, Math.max(p.min!, +x.toFixed(6)));
    }
  }
  const fps = FPS.includes(c.fpsCap as 15) ? (c.fpsCap as BgConfig['fpsCap']) : DEFAULT_BG.fpsCap;
  return {
    id: d.id,
    seed: Math.floor(num(c.seed, 0, 0xffffffff, DEFAULT_BG.seed)),
    params, intensity: num(c.intensity, 0, 1, DEFAULT_BG.intensity), speed: num(c.speed, 0, 2, DEFAULT_BG.speed), fpsCap: fps,
  };
}
