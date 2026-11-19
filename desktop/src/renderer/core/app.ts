// App-wide reactive state wired to the bridge: backend state, settings, app state, live health, the topic store.
import { signal, computed, effect } from '@preact/signals';
import { useEffect } from 'preact/hooks';
import type { ActionResult, Topic, TopicData } from '../../shared/api';
import type { AppInfo, AppState, BackendState, Settings, DeepPartial, KeyVaultState, AppUpdateState } from '../../shared/ipc';
import { bridge, on, api } from './bridge';
import { TopicStore, topicPath, type TopicEntry } from './store';
import { toast } from './toast';

export const backend = signal<BackendState | null>(null);
export const settings = signal<Settings | null>(null);
export const appState = signal<AppState | null>(null);
export const appInfo = signal<AppInfo | null>(null);
export const liveHealth = signal<{ ok: boolean; failures: number; nextInMs: number }>({ ok: true, failures: 0, nextInMs: 0 });
export const keyState = signal<KeyVaultState | null>(null);
export const updateState = signal<AppUpdateState | null>(null);
export const windowState = signal({ maximized: false, fullscreen: false, focused: true, visible: true });
export const online = computed(() => backend.value?.phase === 'up' && liveHealth.value.ok);
export const offlineReason = computed(() => {
  const b = backend.value;
  if (!b) return 'backend starting';
  if (b.phase !== 'up') return `backend ${b.phase}`;
  if (!liveHealth.value.ok) return 'backend offline';
  return '';
});
export const platform = computed(() => appInfo.value?.platform ?? (navigator.platform.toLowerCase().includes('mac') ? 'darwin' : 'linux'));

export const store = new TopicStore({
  setTopics: (t) => { void bridge.live.set(t).catch(() => {}); },
  fetch: (t, sig) => bridge.api.get(api(topicPath(t)), undefined, sig || undefined),
  now: () => Date.now(),
});

/** Hook: subscribe while mounted. */
export function useTopic<T extends Topic>(t: T | null): TopicEntry<TopicData<T>> | null {
  useEffect(() => (t ? store.subscribe(t) : undefined), [t]);
  return t ? store.entry(t) : null;
}

let started = false;
export async function startApp(): Promise<void> {
  if (started) return;
  started = true;
  on('backend:state', (s) => {
    const prev = backend.value?.phase;
    backend.value = s;
    if (s.phase === 'up' && prev && prev !== 'up') { store.resubscribe(); for (const t of store.active()) void store.load(t); }
    store.markAllStale(s.phase !== 'up');
  });
  on('live:update', ({ topic, sig, data }) => {
    const d = data as { error?: { code: string; message: string } } | undefined;
    if (d && typeof d === 'object' && 'error' in d && d.error && Object.keys(d).length === 1) store.fail(topic, d.error as never);
    else store.apply(topic, sig, data);
  });
  on('live:health', (h) => { liveHealth.value = h; store.markAllStale(!h.ok); });
  on('settings:changed', (s) => { settings.value = s; });
  on('keys:changed', (k) => { keyState.value = k; });
  on('update:state', (u) => { updateState.value = u; });
  on('window:state', (w) => { windowState.value = w; });
  const settle = <T,>(p: Promise<T>) => p.then((v) => v, () => null);
  const [info, b, s, st, k, u] = await Promise.all([
    settle(bridge.app.info()), settle(bridge.backend.get()), settle(bridge.settings.get()), settle(bridge.state.get()),
    settle(bridge.keys.list()), settle(bridge.update.get()),
  ]);
  if (info && 'version' in (info as object)) appInfo.value = info;
  if (b && 'phase' in (b as object)) backend.value = b;
  if (s && 'appearance' in (s as object)) settings.value = s;
  if (st && 'version' in (st as object)) appState.value = st;
  if (k && 'keys' in (k as object)) keyState.value = k;
  if (u && 'phase' in (u as object)) updateState.value = u;
}

export async function patchSettings(p: DeepPartial<Settings>): Promise<void> {
  try { const s = await bridge.settings.set(p); if (s && 'appearance' in s) settings.value = s; } catch { /* stays as is */ }
}

let stateTimer: ReturnType<typeof setTimeout> | null = null;
let pending: Partial<AppState> = {};
/** Debounced (500 ms) app-state write (W20). */
export function patchState(p: Partial<AppState>, now = false): void {
  pending = { ...pending, ...p };
  if (appState.value) appState.value = { ...appState.value, ...p };
  if (stateTimer) clearTimeout(stateTimer);
  const flush = () => { const x = pending; pending = {}; stateTimer = null; void bridge.state.set(x).catch(() => {}); };
  if (now) flush(); else stateTimer = setTimeout(flush, 500);
}
export function flushState(): void { if (stateTimer) { clearTimeout(stateTimer); const x = pending; pending = {}; stateTimer = null; void bridge.state.set(x).catch(() => {}); } }

/**
 * POST an action. Never retried (W5). Success -> toast with the server msg; refusal -> returned for inline
 * display; 'unknown-result' -> toast "Result unknown, state refreshed" and a reload of the given topics.
 */
export async function act(path: string, body: Record<string, unknown> = {}, opts: { refresh?: Topic[]; quiet?: boolean } = {}): Promise<ActionResult> {
  let r: ActionResult;
  try { r = await bridge.api.post(api(path), body); } catch (e) {
    r = { ok: false, status: 0, error: { code: 'unknown-result', message: String((e as Error)?.message || e) } };
  }
  if (r.ok) { if (!opts.quiet) toast({ kind: 'ok', title: r.msg || 'Done' }); }
  else if (r.error.code === 'unknown-result' || r.error.code === 'timeout' || r.error.code === 'offline') {
    toast({ kind: 'warn', title: 'Result unknown, state refreshed', body: r.error.message });
  }
  for (const t of opts.refresh ?? []) void store.load(t);
  return r;
}

/** Apply appearance settings to <html> data attributes (3.1 States). */
export function bindAppearance(): void {
  const mq = window.matchMedia('(prefers-color-scheme: light)');
  const rm = window.matchMedia('(prefers-reduced-motion: reduce)');
  const apply = () => {
    const a = settings.value?.appearance;
    const h = document.documentElement;
    const theme = !a || a.theme === 'system' ? (mq.matches ? 'light' : 'dark') : a.theme;
    h.dataset.theme = theme;
    h.dataset.accent = a?.accent || 'iris';
    h.dataset.density = a?.density || 'compact';
    h.dataset.glass = a?.glass === false ? 'off' : 'on';
    h.dataset.seeThrough = a && a.seeThrough > 0 ? '1' : '0';
    const reduce = a?.reduceMotion === 'on' || ((!a || a.reduceMotion === 'system') && rm.matches);
    h.dataset.reduceMotion = reduce ? '1' : '0';
    if (a?.uiScale && a.uiScale !== 1) h.style.zoom = String(a.uiScale); else h.style.zoom = '';
  };
  effect(apply);
  mq.addEventListener('change', apply);
  rm.addEventListener('change', apply);
  effect(() => { document.documentElement.dataset.phase = backend.value?.phase ?? 'locating'; });
}
export const reducedMotion = () => document.documentElement.dataset.reduceMotion === '1';
