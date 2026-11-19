// Main <-> worker messages, with validation on both ends. Pure.
import type { BgConfig, BgRunState, BgTheme } from '../shared/bg';

export type RendererKind = 'webgl2' | 'worker2d';

export type ToWorker =
  | { t: 'init'; id: number; canvas: OffscreenCanvas; w: number; h: number; config: BgConfig; allowGL: boolean }
  | { t: 'config'; id: number; config: BgConfig }
  | { t: 'resize'; id: number; w: number; h: number }
  | { t: 'drop'; id: number }
  | { t: 'theme'; theme: BgTheme }
  | { t: 'state'; state: BgRunState; reduced: boolean }
  | { t: 'thumb'; req: number; config: BgConfig; w: number; h: number; theme: BgTheme }
  | { t: 'debug'; burnMs: number };

export type FromWorker =
  | { t: 'ready'; id: number; renderer: RendererKind }
  | { t: 'stats'; id: number; fps: number; frameMs: number; renderer: RendererKind; tier: 0 | 1 | 2 | 3; downgraded: boolean; reason: string; frames: number }
  | { t: 'fail'; id: number; reason: string; contextLost: boolean }
  | { t: 'thumb'; req: number; blob: Blob | null; error: string };

const isObj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
const isInt = (v: unknown, lo = 0, hi = 0x7fffffff) => typeof v === 'number' && Number.isInteger(v) && v >= lo && v <= hi;
const isDim = (v: unknown) => isInt(v, 1, 16384);
const isNum = (v: unknown) => typeof v === 'number' && Number.isFinite(v);
const RUN: BgRunState[] = ['run', 'dim', 'pause'];

export function isConfig(v: unknown): v is BgConfig {
  if (!isObj(v) || typeof v.id !== 'string' || !isNum(v.seed) || !isNum(v.intensity) || !isNum(v.speed)) return false;
  if (![15, 24, 30, 60].includes(v.fpsCap as number) || !isObj(v.params)) return false;
  return Object.values(v.params).every((x) => typeof x === 'number' || typeof x === 'string' || typeof x === 'boolean');
}
export function isTheme(v: unknown): v is BgTheme {
  return isObj(v) && (v.mode === 'dark' || v.mode === 'light')
    && ['base', 'fg', 'accent', 'accent2'].every((k) => typeof v[k] === 'string' && (v[k] as string).length < 128);
}

/** Validate a message arriving in the worker. Returns null for anything malformed. */
export function parseToWorker(v: unknown): ToWorker | null {
  if (!isObj(v) || typeof v.t !== 'string') return null;
  switch (v.t) {
    case 'init':
      return isInt(v.id) && typeof OffscreenCanvas !== 'undefined' && v.canvas instanceof OffscreenCanvas
        && isDim(v.w) && isDim(v.h) && isConfig(v.config) && typeof v.allowGL === 'boolean' ? (v as ToWorker) : null;
    case 'config': return isInt(v.id) && isConfig(v.config) ? (v as ToWorker) : null;
    case 'resize': return isInt(v.id) && isDim(v.w) && isDim(v.h) ? (v as ToWorker) : null;
    case 'drop': return isInt(v.id) ? (v as ToWorker) : null;
    case 'theme': return isTheme(v.theme) ? (v as ToWorker) : null;
    case 'state': return RUN.includes(v.state as BgRunState) && typeof v.reduced === 'boolean' ? (v as ToWorker) : null;
    case 'thumb': return isInt(v.req) && isConfig(v.config) && isInt(v.w, 1, 1024) && isInt(v.h, 1, 1024) && isTheme(v.theme) ? (v as ToWorker) : null;
    case 'debug': return isNum(v.burnMs) && (v.burnMs as number) >= 0 && (v.burnMs as number) <= 100 ? (v as ToWorker) : null;
    default: return null;
  }
}

/** Validate a message arriving on the main thread. */
export function parseFromWorker(v: unknown): FromWorker | null {
  if (!isObj(v) || typeof v.t !== 'string') return null;
  const rk = (x: unknown) => x === 'webgl2' || x === 'worker2d';
  switch (v.t) {
    case 'ready': return isInt(v.id) && rk(v.renderer) ? (v as FromWorker) : null;
    case 'stats':
      return isInt(v.id) && isNum(v.fps) && isNum(v.frameMs) && rk(v.renderer) && isInt(v.tier, 0, 3)
        && typeof v.downgraded === 'boolean' && typeof v.reason === 'string' && isInt(v.frames, 0, Number.MAX_SAFE_INTEGER) ? (v as FromWorker) : null;
    case 'fail': return isInt(v.id) && typeof v.reason === 'string' && typeof v.contextLost === 'boolean' ? (v as FromWorker) : null;
    case 'thumb':
      return isInt(v.req) && (v.blob === null || (typeof Blob !== 'undefined' && v.blob instanceof Blob)) && typeof v.error === 'string' ? (v as FromWorker) : null;
    default: return null;
  }
}
