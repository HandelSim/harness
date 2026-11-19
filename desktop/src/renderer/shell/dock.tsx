// dockview-core integration (PLAN 3.2, W4): content/tab renderers that mount lazy Preact panel components,
// layout persistence (W20), reopen stack, layout undo, keyboard group navigation.
import { createDockview, type DockviewApi, type IContentRenderer, type ITabRenderer, type IWatermarkRenderer,
  type GroupPanelPartInitParameters, type TabPartInitParameters, type IDockviewPanel, type DockviewTheme } from 'dockview-core';
import { render, type ComponentType } from 'preact';
import { signal, type Signal, effect } from '@preact/signals';
import { panelId, parsePanelId, validateParams, isKeyed, ReopenStack, SnapshotStack, VIEW_TITLES, VIEW_WEB, uid,
  type PanelKind, type ViewId } from './panels';
import { appState, patchState } from '../core/app';

export type TabState = 'idle' | 'starting' | 'running' | 'working' | 'error' | 'ended' | 'attention';
export interface PanelCtx {
  id: string; kind: PanelKind; key: string;
  params: Signal<Record<string, unknown>>;
  visible: Signal<boolean>;
  active: Signal<boolean>;
  setTitle(t: string): void;
  setTabState(s: TabState): void;
  /** Merge into params (persisted with the layout). */
  update(p: Record<string, unknown>): void;
  close(): void;
  /** Return false (or a promise of false) to veto a close (e.g. live terminal confirm). */
  beforeClose(fn: () => boolean | Promise<boolean>): void;
  onShow(fn: () => void): void;
  onHide(fn: () => void): void;
}
export type PanelComponent = ComponentType<{ ctx: PanelCtx }>;

type Loader = () => Promise<PanelComponent>;
const loaders = new Map<string, Loader>();
/** Register a lazy panel component for a dockview component name ("view.queue", "terminal", ...). */
export function registerPanel(component: string, load: Loader): void { loaders.set(component, load); }

const tabStates = new Map<string, Signal<TabState>>();
export function tabState(id: string): Signal<TabState> {
  let s = tabStates.get(id);
  if (!s) { s = signal<TabState>('idle'); tabStates.set(id, s); }
  return s;
}
interface Hooks { beforeClose: (() => boolean | Promise<boolean>)[]; show: (() => void)[]; hide: (() => void)[] }
const hooks = new Map<string, Hooks>();
const ctxs = new Map<string, PanelCtx>();

let api: DockviewApi | null = null;
export const dockReady = signal(false);
export const activePanelId = signal<string | null>(null);
export const panelsVersion = signal(0);
const reopen = new ReopenStack();
const snapshots = new SnapshotStack(20);
let restoring = false;

class Content implements IContentRenderer {
  readonly element = document.createElement('div');
  private ctx: PanelCtx | null = null;
  constructor(private component: string, private id: string) { this.element.className = 'panel-body'; }
  init(p: GroupPanelPartInitParameters): void {
    const ref = parsePanelId(this.id);
    const kind = (ref?.kind ?? (this.component === 'view' ? 'view' : this.component)) as PanelKind;
    const params = validateParams(kind, p.params);
    this.element.dataset.testid = `panel-${this.id}`;
    if (ref?.kind === 'view') this.element.dataset.view = ref.key;
    if (!ref || !params) { this.element.textContent = ''; setTimeout(() => api?.getPanel(this.id)?.api.close(), 0); return; }
    const h: Hooks = { beforeClose: [], show: [], hide: [] };
    hooks.set(this.id, h);
    const ctx: PanelCtx = {
      id: this.id, kind: ref.kind, key: ref.key,
      params: signal(params), visible: signal(p.api.isVisible), active: signal(p.api.isActive),
      setTitle: (t) => { if (p.api.title !== t) p.api.setTitle(t); },
      setTabState: (s) => { tabState(this.id).value = s; },
      update: (x) => { ctx.params.value = { ...ctx.params.value, ...x }; p.api.updateParameters(ctx.params.value); scheduleSave(); },
      close: () => { void requestClose(this.id); },
      beforeClose: (fn) => h.beforeClose.push(fn), onShow: (fn) => h.show.push(fn), onHide: (fn) => h.hide.push(fn),
    };
    this.ctx = ctx;
    ctxs.set(this.id, ctx);
    p.api.onDidVisibilityChange((e) => { ctx.visible.value = e.isVisible; for (const f of e.isVisible ? h.show : h.hide) f(); });
    p.api.onDidActiveChange((e) => { ctx.active.value = e.isActive; });
    const name = ref.kind === 'view' ? `view.${ref.key}` : ref.kind;
    const load = loaders.get(name) ?? (ref.kind === 'view' ? loaders.get('web') : undefined);
    if (!load) { this.element.textContent = `Unknown panel kind ${name}`; return; }
    if (!loaders.get(name) && ref.kind === 'view') ctx.params.value = { ...ctx.params.value, path: VIEW_WEB[ref.key as ViewId], fallbackFor: ref.key };
    load().then((C) => { if (this.ctx) render(<C ctx={ctx} />, this.element); },
      (err) => { this.element.textContent = `Could not load panel: ${String(err?.message || err)}`; });
  }
  dispose(): void {
    render(null, this.element);
    hooks.delete(this.id); ctxs.delete(this.id); tabStates.delete(this.id);
    this.ctx = null;
  }
}

class Tab implements ITabRenderer {
  readonly element = document.createElement('div');
  private stop: (() => void) | null = null;
  constructor(private id: string) {}
  init(p: TabPartInitParameters): void {
    const el = this.element;
    el.className = 'htab';
    el.dataset.testid = `tab-${this.id}`;
    const dot = document.createElement('span');
    const label = document.createElement('span');
    label.className = 'htab-label';
    const close = document.createElement('button');
    close.className = 'htab-close';
    close.type = 'button';
    close.tabIndex = -1;
    close.setAttribute('aria-label', 'Close tab');
    close.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18"/></svg>';
    close.addEventListener('pointerdown', (e) => e.stopPropagation());
    close.addEventListener('click', (e) => { e.stopPropagation(); void requestClose(this.id); });
    el.addEventListener('auxclick', (e) => { if (e.button === 1) { e.preventDefault(); void requestClose(this.id); } });
    el.append(dot, label, close);
    const setTitle = (t: string | undefined) => { label.textContent = t || this.id; el.title = t || this.id; };
    setTitle(p.api.title);
    p.api.onDidTitleChange((e) => setTitle(e.title));
    this.stop = effect(() => { const s = tabState(this.id).value; dot.className = `htab-dot htab-dot--${s}`; });
  }
  dispose(): void { this.stop?.(); }
}

class Watermark implements IWatermarkRenderer {
  readonly element = document.createElement('div');
  init(): void {
    this.element.className = 'dock-watermark';
    const mod = navigator.platform.toLowerCase().includes('mac') ? '⌘' : 'Ctrl+';
    this.element.innerHTML = '';
    const wrap = document.createElement('div');
    wrap.className = 'dock-watermark-inner';
    for (const [k, t] of [[`${mod}T`, 'New session'], [`${mod}K`, 'Command palette'], ['Alt+2', 'Queue']]) {
      const row = document.createElement('div');
      row.className = 'row';
      const kbd = document.createElement('kbd'); kbd.textContent = k;
      const s = document.createElement('span'); s.textContent = t;
      row.append(s, kbd);
      wrap.append(row);
    }
    this.element.append(wrap);
  }
}

export const harnessTheme: DockviewTheme = {
  name: 'harness', className: 'dockview-theme-harness', gap: 6, dndOverlayMounting: 'absolute', dndPanelOverlay: 'group',
};

export function mountDock(el: HTMLElement): DockviewApi {
  api = createDockview(el, {
    theme: harnessTheme,
    createComponent: (o) => new Content(o.name, o.id),
    defaultTabComponent: 'harness',
    createTabComponent: (o) => new Tab(o.id),
    createWatermarkComponent: () => new Watermark(),
    singleTabMode: 'default',
  });
  api.onDidActivePanelChange((e) => { activePanelId.value = e.panel?.id ?? null; });
  api.onDidAddPanel(() => { panelsVersion.value++; });
  api.onDidRemovePanel((p) => { panelsVersion.value++; if (!restoring) rememberClosed(p); });
  api.onDidLayoutChange(() => scheduleSave());
  const saved = appState.value?.dock;
  restoring = true;
  try {
    if (saved && typeof saved === 'object') api.fromJSON(saved as never);
  } catch { api.clear(); }
  restoring = false;
  if (!api.panels.length) openView('home');
  dockReady.value = true;
  return api;
}

let saveTimer: ReturnType<typeof setTimeout> | null = null;
function scheduleSave(): void {
  if (!api || restoring) return;
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    if (!api) return;
    const json = api.toJSON();
    snapshots.push(JSON.stringify(json));
    patchState({ dock: json });
  }, 300);
}
export function saveLayoutNow(): void { if (api) patchState({ dock: api.toJSON() }, true); }

function rememberClosed(p: IDockviewPanel): void {
  const ref = parsePanelId(p.id);
  if (!ref) return;
  reopen.push({ id: p.id, kind: ref.kind, params: (p.params ?? {}) as Record<string, unknown>, title: p.title ?? p.id });
  patchState({ closed: reopen.items });
}

export function getApi(): DockviewApi | null { return api; }

export interface OpenOpts { split?: 'right' | 'down'; title?: string; params?: Record<string, unknown>; floating?: boolean; inactive?: boolean }
/** Open (or focus, for keyed kinds) a panel. Returns its id. */
export function openPanel(kind: PanelKind, key: string | null, opts: OpenOpts = {}): string {
  if (!api) return '';
  const k = key ?? uid();
  const id = panelId(kind, k);
  const existing = api.getPanel(id);
  if (existing && (isKeyed(kind) || key)) {
    if (opts.params) existing.api.updateParameters({ ...(existing.params ?? {}), ...opts.params });
    existing.api.setActive();
    return id;
  }
  const ref = api.activePanel;
  const position = opts.split && ref ? { referencePanel: ref, direction: opts.split === 'right' ? 'right' as const : 'below' as const } : undefined;
  api.addPanel({ id, component: kind === 'view' ? `view.${k}` : kind, title: opts.title ?? (kind === 'view' ? VIEW_TITLES[k as ViewId] : k),
    params: { ...(opts.params ?? {}) }, position, inactive: opts.inactive, floating: opts.floating || undefined } as never);
  return id;
}
export function openView(v: ViewId, split?: boolean): string { return openPanel('view', v, { split: split ? 'right' : undefined }); }
export function openWeb(path: string, opts: OpenOpts = {}): string {
  return openPanel('web', null, { ...opts, title: opts.title ?? path, params: { ...(opts.params ?? {}), path } });
}

export async function requestClose(id: string): Promise<void> {
  const p = api?.getPanel(id);
  if (!p) return;
  for (const f of hooks.get(id)?.beforeClose ?? []) { if (!(await f())) return; }
  p.api.close();
}
export function closeActive(): void { const p = api?.activePanel; if (p) void requestClose(p.id); }
export function closeOthers(id: string, toRight = false): void {
  const p = api?.getPanel(id);
  if (!p) return;
  const list = p.group.panels;
  const i = list.indexOf(p);
  for (const [j, x] of list.entries()) if (x !== p && (!toRight || j > i)) void requestClose(x.id);
}

export function reopenClosed(): boolean {
  const x = reopen.pop();
  if (!x || !api) return false;
  patchState({ closed: reopen.items });
  const ref = parsePanelId(x.id);
  if (!ref || !validateParams(ref.kind, x.params)) return false;
  openPanel(ref.kind, ref.kind === 'terminal' || ref.kind === 'web' || ref.kind === 'chat' ? uid() : ref.key, { title: x.title, params: x.params });
  return true;
}

export function undoLayout(): boolean {
  const s = snapshots.undo();
  if (!s || !api) return false;
  restoring = true;
  try { api.fromJSON(JSON.parse(s)); } catch { /* keep current */ }
  restoring = false;
  patchState({ dock: api.toJSON() });
  return true;
}
export function resetLayout(): void {
  if (!api) return;
  api.clear();
  openView('home');
}

export function focusGroup(dir: 'left' | 'right' | 'up' | 'down', move = false): void {
  if (!api) return;
  const g = api.activeGroup;
  if (!g) return;
  const n = api.adjacentGroupInDirection(g, dir);
  if (!n) return;
  if (move && api.activePanel) api.activePanel.api.moveTo({ group: n as never });
  else n.activePanel?.api.setActive();
}
export function cycleTab(delta: 1 | -1): void {
  const p = api?.activePanel;
  if (!p) return;
  const list = p.group.panels;
  const i = list.indexOf(p);
  list[(i + delta + list.length) % list.length]?.api.setActive();
}
export function tabN(n: number): void {
  const g = api?.activeGroup;
  if (!g) return;
  const list = g.panels;
  (n >= 9 ? list[list.length - 1] : list[n - 1])?.api.setActive();
}
export function splitActive(dir: 'right' | 'down'): void {
  const p = api?.activePanel;
  if (!p || !api) return;
  const ref = parsePanelId(p.id);
  if (!ref) return;
  if (isKeyed(ref.kind)) { api.addGroup({ referencePanel: p, direction: dir === 'right' ? 'right' : 'below' }); p.api.moveTo({ group: api.activeGroup! as never }); return; }
  openPanel(ref.kind, null, { split: dir, title: p.title, params: (p.params ?? {}) as Record<string, unknown> });
}
export function floatActive(): void { const p = api?.activePanel; if (p && api) api.addFloatingGroup(p); }
export function panelCtx(id: string): PanelCtx | undefined { return ctxs.get(id); }
export function listPanels(): { id: string; title: string; group: string }[] {
  return (api?.panels ?? []).map((p) => ({ id: p.id, title: p.title ?? p.id, group: p.group.id }));
}
