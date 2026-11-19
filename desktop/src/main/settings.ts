// Settings defaults, validation and env (PLAN 6.4). The ONLY place defaults for paths/ports/URLs live.
import type { Settings } from '../shared/ipc';
import type { BgConfig } from '../shared/bg';
import { shapeMerge, isObj } from './store';

export const DEFAULT_PORT = 8780;
export const DEFAULT_UPDATE_URL = 'https://github.com/HandelSim/harness/releases/download/desktop-latest/';
export const RELEASE_PAGE = 'https://github.com/HandelSim/harness/releases/tag/desktop-latest';
export const DEFAULT_BG_CONFIG: BgConfig = { id: 'mesh', seed: 1, params: {}, intensity: 0.6, speed: 1, fpsCap: 30 };

export function defaultSettings(): Settings {
  return {
    appearance: { theme: 'dark', accent: '#7c9cff', density: 'comfortable', uiScale: 1, glass: true, seeThrough: 0.5,
      reduceMotion: 'system' },
    background: { config: { ...DEFAULT_BG_CONFIG, params: {} }, onBlur: 'dim', onBattery: 'static', rotate: 'never' },
    terminal: { fontFamily: 'JetBrains Mono Variable, monospace', fontSize: 13, lineHeight: 1.2, cursorBlink: true,
      scrollback: 10000, confirmClose: true, confirmPaste: true, webgl: true },
    sessions: { restore: 'tabs', defaultKind: 'agent' },
    queue: { startMode: 'container' },
    backend: { installRoot: null, port: DEFAULT_PORT, onQuit: 'keep-if-busy', hostConsent: false },
    updates: { auto: true, harnessCheck: true },
    notifications: { needsInput: true, ticketDone: true, runEnded: true, key: true, update: true },
    keymap: {},
  };
}

const ENUMS: Record<string, readonly string[]> = {
  'appearance.theme': ['dark', 'light', 'system'], 'appearance.density': ['comfortable', 'compact'],
  'appearance.reduceMotion': ['system', 'on', 'off'], 'background.onBlur': ['run', 'dim', 'pause'],
  'background.onBattery': ['run', 'static'], 'background.rotate': ['never', 'daily', 'launch'],
  'sessions.restore': ['tabs', 'tabs-and-resume', 'none'], 'sessions.defaultKind': ['agent', 'host', 'shell', 'plain'],
  'queue.startMode': ['container', 'host', 'single'], 'backend.onQuit': ['keep-if-busy', 'stop', 'keep'],
};
const RANGES: Record<string, [number, number]> = {
  'appearance.uiScale': [0.75, 1.5], 'appearance.seeThrough': [0, 1], 'terminal.fontSize': [8, 32],
  'terminal.lineHeight': [1, 2], 'terminal.scrollback': [500, 200000], 'backend.port': [1024, 65535],
};
const FREE = new Set(['keymap', 'background.config.params']);

/** Deep-merge a patch, then clamp/repair every field. Unknown keys are dropped. */
export function mergeSettings(base: Settings, patch: unknown): Settings {
  const s = shapeMerge(base, patch, FREE);
  return validateSettings(s, base);
}

export function validateSettings(s: Settings, fallback: Settings = defaultSettings()): Settings {
  const get = (o: Record<string, unknown>, p: string): unknown => p.split('.').reduce<unknown>((a, k) => (isObj(a) ? a[k] : undefined), o);
  const set = (o: Record<string, unknown>, p: string, v: unknown) => {
    const ks = p.split('.'); let cur = o;
    for (const k of ks.slice(0, -1)) cur = cur[k] as Record<string, unknown>;
    cur[ks[ks.length - 1]] = v;
  };
  const o = s as unknown as Record<string, unknown>;
  const f = fallback as unknown as Record<string, unknown>;
  for (const [p, vals] of Object.entries(ENUMS)) if (!vals.includes(get(o, p) as string)) set(o, p, get(f, p));
  for (const [p, [lo, hi]] of Object.entries(RANGES)) {
    const v = get(o, p);
    if (typeof v !== 'number' || !Number.isFinite(v)) set(o, p, get(f, p));
    else set(o, p, Math.min(hi, Math.max(lo, v)));
  }
  s.backend.port = Math.round(s.backend.port);
  if (s.backend.installRoot !== null && typeof s.backend.installRoot !== 'string') s.backend.installRoot = null;
  if (!/^#[0-9a-fA-F]{6}$/.test(s.appearance.accent)) s.appearance.accent = fallback.appearance.accent;
  const km: Record<string, string> = {};
  for (const [k, v] of Object.entries(s.keymap || {})) if (typeof v === 'string' && k.length < 100 && v.length < 100) km[k] = v;
  s.keymap = km;
  const bg = s.background.config;
  if (typeof bg.id !== 'string' || !bg.id) s.background.config = { ...DEFAULT_BG_CONFIG, params: {} };
  bg.intensity = clamp(bg.intensity, 0, 1, DEFAULT_BG_CONFIG.intensity);
  bg.speed = clamp(bg.speed, 0, 2, 1);
  if (![15, 24, 30, 60].includes(bg.fpsCap)) bg.fpsCap = 30;
  const params: Record<string, number | string | boolean> = {};
  for (const [k, v] of Object.entries(bg.params || {})) if (['number', 'string', 'boolean'].includes(typeof v)) params[k] = v as never;
  bg.params = params;
  return s;
}

function clamp(v: unknown, lo: number, hi: number, d: number): number {
  return typeof v === 'number' && Number.isFinite(v) ? Math.min(hi, Math.max(lo, v)) : d;
}

export function coerceSettings(raw: unknown): Settings | null {
  if (!isObj(raw)) return null;
  return mergeSettings(defaultSettings(), raw);
}

/** Environment (6.4). Nothing else is read from process.env. */
export interface Env {
  root: string | null; serveUrl: string | null; servePort: number | null; userData: string | null;
  updateUrl: string | null; noUpdate: boolean; disableGpu: boolean; logLevel: string; e2e: boolean;
}
export function readEnv(e: NodeJS.ProcessEnv = process.env): Env {
  const port = Number(e.HARNESS_DESKTOP_SERVE_PORT);
  return {
    root: e.HARNESS_DESKTOP_ROOT || null,
    serveUrl: e.HARNESS_DESKTOP_SERVE_URL || null,
    servePort: Number.isInteger(port) && port > 0 && port < 65536 ? port : null,
    userData: e.HARNESS_DESKTOP_USER_DATA || null,
    updateUrl: e.HARNESS_DESKTOP_UPDATE_URL || null,
    noUpdate: e.HARNESS_DESKTOP_NO_UPDATE === '1',
    disableGpu: e.HARNESS_DESKTOP_DISABLE_GPU === '1',
    logLevel: e.HARNESS_DESKTOP_LOG_LEVEL || 'info',
    e2e: e.HARNESS_DESKTOP_E2E === '1',
  };
}
