// AppState (window, dock layout, panels, drafts, recents): defaults and coercion.
import type { AppState } from '../shared/ipc';
import { isObj } from './store';

export function defaultState(): AppState {
  return {
    version: 1,
    window: { width: 1400, height: 900, maximized: false, fullscreen: false },
    shell: { sidebar: 280, sidebarOpen: true, collapsed: {}, activeView: 'home' },
    dock: null, panels: {}, closed: [], drafts: {}, recents: { projects: [], commands: [] },
  };
}

export function coerceState(raw: unknown): AppState | null {
  if (!isObj(raw) || raw.version !== 1) return null;
  return mergeState(defaultState(), raw);
}

/** Top-level replace per key (the renderer owns the inner shapes), with light type checks. */
export function mergeState(base: AppState, patch: unknown): AppState {
  if (!isObj(patch)) return base;
  const s: AppState = { ...base };
  const w = patch.window;
  if (isObj(w)) {
    const n = (v: unknown, d: number | undefined) => (typeof v === 'number' && Number.isFinite(v) ? Math.round(v) : d);
    s.window = {
      x: n(w.x, base.window.x), y: n(w.y, base.window.y),
      width: Math.max(480, n(w.width, base.window.width) ?? 1400), height: Math.max(320, n(w.height, base.window.height) ?? 900),
      maximized: typeof w.maximized === 'boolean' ? w.maximized : base.window.maximized,
      fullscreen: typeof w.fullscreen === 'boolean' ? w.fullscreen : base.window.fullscreen,
    };
  }
  if (isObj(patch.shell)) s.shell = { ...base.shell, ...(patch.shell as object) } as AppState['shell'];
  if ('dock' in patch) s.dock = patch.dock ?? null;
  if (isObj(patch.panels)) s.panels = patch.panels;
  if (Array.isArray(patch.closed)) s.closed = patch.closed.slice(-20);
  if (isObj(patch.drafts)) {
    const d: Record<string, string> = {};
    for (const [k, v] of Object.entries(patch.drafts)) if (typeof v === 'string' && v.length <= 200000) d[k] = v;
    s.drafts = d;
  }
  if (isObj(patch.recents)) {
    const r = patch.recents;
    const strs = (a: unknown, d: string[]) => (Array.isArray(a) ? a.filter((x): x is string => typeof x === 'string').slice(0, 50) : d);
    s.recents = { projects: strs(r.projects, base.recents.projects), commands: strs(r.commands, base.recents.commands) };
  }
  return s;
}
