// The shell (PLAN 3.1): title bar, rail, sidebar, dock, status bar, overlays.
import { useEffect, useRef } from 'preact/hooks';
import { backend, online, appState, keyState, platform, store, useTopic, liveHealth, windowState } from '../core/app';
import { bridge } from '../core/bridge';
import { toasts, dismiss } from '../core/toast';
import { Icon } from '../ui/Icon';
import { VIEW_IDS, VIEW_TITLES, type ViewId } from './panels';
import { mountDock, openView, openPanel, activePanelId, panelsVersion, listPanels, getApi } from './dock';
import { paletteOpen, dialog, popover, zen, sidebarOpen, setSidebarWidth, closeDialog } from './ui';
import { Palette } from './Palette';
import { formatChord } from '../core/keymap';
import { bgStats, bgError } from '../core/background';
import type { Queue } from '../../shared/api';
import { BackendPopover } from './BackendPopover';
import { BgPicker } from '../views/background/BgPicker';
import { Onboarding } from './Onboarding';

const RAIL: ViewId[] = ['home', 'queue', 'sessions', 'findings', 'vault', 'jira', 'debug'];
const RAIL_BOTTOM: ViewId[] = ['keys', 'updates', 'settings'];

function TitleBar() {
  const mac = platform.value === 'darwin';
  const b = backend.value;
  return (
    <header class="titlebar" style={mac ? { paddingLeft: '78px' } : undefined}>
      <div class="brand"><span class="brand-mark" />harness</div>
      <button class="palette-pill" onClick={() => { paletteOpen.value = { prefix: '' }; }} aria-label="Open command palette">
        <Icon name="search" size={14} /><span>Search or run a command</span><kbd>{formatChord('Mod+K', platform.value)}</kbd>
      </button>
      <button class={`btn btn--sm btn--ghost`} data-testid="backend-chip" onClick={() => { popover.value = popover.value === 'backend' ? null : 'backend'; }}>
        <span class={`dot ${online.value ? 'dot--ok' : b?.phase === 'starting' || b?.phase === 'reconnecting' || b?.phase === 'locating' ? 'dot--warn' : 'dot--err'}`} />
        {b?.phase === 'up' ? (b.owned ? 'Local' : 'Attached') : b?.phase ?? 'locating'}
      </button>
      {!mac ? (
        <div class="winctl">
          <button aria-label="Minimize" onClick={() => void bridge.window.action('minimize')}><svg width="10" height="10" viewBox="0 0 10 10"><path d="M1 5h8" stroke="currentColor" /></svg></button>
          <button aria-label="Maximize" onClick={() => void bridge.window.action('maximize')}><svg width="10" height="10" viewBox="0 0 10 10"><rect x="1.5" y="1.5" width="7" height="7" fill="none" stroke="currentColor" /></svg></button>
          <button aria-label="Close" class="close" onClick={() => void bridge.window.action('close')}><svg width="10" height="10" viewBox="0 0 10 10"><path d="M1.5 1.5l7 7M8.5 1.5l-7 7" stroke="currentColor" /></svg></button>
        </div>
      ) : null}
    </header>
  );
}

function railBadge(v: ViewId, q: Queue | undefined): { n: number; warn?: boolean } | null {
  if (v === 'queue' && q) {
    const n = q.tickets.filter((t) => t.status === 'needs-answers' || t.status === 'plan-review' || t.status === 'blocked').length;
    return n ? { n, warn: true } : null;
  }
  return null;
}

function Rail() {
  void panelsVersion.value;
  const active = activePanelId.value;
  const q = useTopic('queue');
  const qd = q?.data.value as Queue | undefined;
  const btn = (v: ViewId) => {
    const b = railBadge(v, qd);
    return (
      <button key={v} data-testid={`rail-${v}`} aria-label={VIEW_TITLES[v]} title={VIEW_TITLES[v]} aria-current={active === `view.${v}`}
        onClick={(e) => openView(v, (e as MouseEvent).altKey)}>
        <Icon name={v} size={18} />
        {b ? <span class={`badge ${b.warn ? 'badge--warn' : ''}`}>{b.n > 99 ? '99+' : b.n}</span> : null}
      </button>
    );
  };
  return (
    <nav class="rail" aria-label="Views">
      {RAIL.map(btn)}
      <div class="rail-spacer" />
      {RAIL_BOTTOM.map(btn)}
    </nav>
  );
}

function Sidebar() {
  void panelsVersion.value;
  const st = appState.value?.shell;
  const width = st?.sidebar ?? 260;
  const open = sidebarOpen();
  const active = activePanelId.value;
  const q = store.entry('queue').data.value as Queue | undefined;
  const drag = useRef<{ x: number; w: number } | null>(null);
  const el = useRef<HTMLDivElement>(null);
  const onDown = (e: PointerEvent) => {
    drag.current = { x: e.clientX, w: width };
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
    (e.target as HTMLElement).dataset.drag = '1';
  };
  const onMove = (e: PointerEvent) => {
    if (!drag.current || !el.current) return;
    const w = Math.max(200, Math.min(420, drag.current.w + e.clientX - drag.current.x));
    el.current.style.width = `${w}px`;
  };
  const onUp = (e: PointerEvent) => {
    if (!drag.current) return;
    const w = drag.current.w + e.clientX - drag.current.x;
    drag.current = null;
    delete (e.target as HTMLElement).dataset.drag;
    setSidebarWidth(w);
  };
  const panels = listPanels();
  const tickets = (q?.tickets ?? []).filter((t) => t.status !== 'done').slice(0, 12);
  return (
    <aside class="sidebar" ref={el} hidden={!open} style={{ width: `${width}px` }} aria-label="Sidebar">
      <div class="sidebar-scroll">
        <section class="sidebar-section">
          <header>Open</header>
          {panels.map((p) => (
            <button key={p.id} class="sidebar-item" aria-current={p.id === active} onClick={() => getApi()?.getPanel(p.id)?.api.setActive()}>
              <span class="ellipsis">{p.title}</span>
            </button>
          ))}
        </section>
        <section class="sidebar-section" style={{ marginTop: 'var(--sp-3)' }}>
          <header onClick={() => openView('queue')}>Queue</header>
          {tickets.length === 0 ? <div class="sidebar-item faint" style={{ cursor: 'default' }}>{q ? 'Nothing queued' : '—'}</div> : null}
          {tickets.map((t) => (
            <button key={t.key} class="sidebar-item" aria-current={active === `ticket:${t.key}`} onClick={() => openPanel('ticket', t.key, { title: t.key, params: { key: t.key } })}>
              <span class={`dot ${t.status === 'running' ? 'dot--live' : t.status === 'needs-answers' || t.status === 'plan-review' ? 'dot--accent' : t.status === 'blocked' ? 'dot--err' : ''}`} />
              <span class="mono small">{t.key}</span><span class="ellipsis muted">{t.title}</span>
            </button>
          ))}
        </section>
      </div>
      <div class="sidebar-resize" onPointerDown={onDown} onPointerMove={onMove} onPointerUp={onUp} role="separator" aria-orientation="vertical" aria-label="Resize sidebar" />
    </aside>
  );
}

function StatusBar() {
  const b = backend.value;
  const q = useTopic('queue');
  const qd = q?.data.value as Queue | undefined;
  const r = qd?.runner;
  const k = keyState.value;
  const activeKey = k?.keys.find((x) => x.active);
  const s = bgStats.value;
  const lh = liveHealth.value;
  return (
    <footer class="statusbar" aria-label="Status">
      <button data-testid="status-backend" onClick={() => { popover.value = popover.value === 'backend' ? null : 'backend'; }}>
        <span class={`dot ${online.value ? 'dot--ok' : 'dot--err'}`} />
        {b?.phase === 'up' ? (lh.ok ? `backend :${b.port}` : 'reconnecting…') : `backend ${b?.phase ?? 'locating'}`}
      </button>
      <button data-testid="status-runner" onClick={() => openView('queue')}>
        <span class={`dot ${r?.running ? 'dot--live' : ''}`} />
        {!r ? 'queue —' : r.halting ? 'queue halting' : r.starting ? 'queue starting' : r.running ? `running ${r.ticket || ''}${r.stage ? ` · ${r.stage}` : ''}` : 'queue idle'}
      </button>
      <span class="spacer" />
      <button data-testid="status-key" onClick={() => openView('keys')}>
        <Icon name="keys" size={12} />{activeKey ? activeKey.label : 'no key'}
      </button>
      <button data-testid="status-bg" onClick={() => { popover.value = popover.value === 'bg' ? null : 'bg'; }} title={bgError.value || (s ? `${s.renderer} · tier ${s.tier}${s.reason ? ` · ${s.reason}` : ''}` : 'background')}>
        <Icon name="layers" size={12} />{s ? `${Math.round(s.fps)} fps` : bgError.value ? 'bg off' : 'bg'}
      </button>
    </footer>
  );
}

function Toasts() {
  return (
    <div class="toasts" aria-live="polite">
      {toasts.value.map((t) => (
        <div key={t.id} class={`toast toast--${t.kind} glass`} data-testid="toast" role={t.kind === 'err' ? 'alert' : 'status'}>
          <span class="t-icon"><Icon name={t.kind === 'ok' ? 'check' : t.kind === 'info' ? 'info' : 'alert'} /></span>
          <div class="grow">
            <div class="t-title">{t.title}</div>
            {t.body ? <div class="t-body">{t.body}</div> : null}
            {t.action ? <button class="btn btn--sm" style={{ marginTop: '6px' }} onClick={() => { t.action!.run(); dismiss(t.id); }}>{t.action.label}</button> : null}
          </div>
          <button class="btn btn--sm btn--ghost btn--icon" aria-label="Dismiss" onClick={() => dismiss(t.id)}><Icon name="x" size={12} /></button>
        </div>
      ))}
    </div>
  );
}

function DialogHost() {
  const d = dialog.value;
  useEffect(() => {
    if (!d) return;
    const k = (e: KeyboardEvent) => { if (e.key === 'Escape') { e.preventDefault(); closeDialog(); } };
    window.addEventListener('keydown', k);
    return () => window.removeEventListener('keydown', k);
  }, [d]);
  if (!d) return null;
  return <><div class="scrim" onClick={closeDialog} />{d}</>;
}

function Dock() {
  const host = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!host.current) return;
    const api = mountDock(host.current);
    document.documentElement.dataset.ready = '1';
    const ro = new ResizeObserver(() => api.layout(host.current!.clientWidth, host.current!.clientHeight));
    ro.observe(host.current);
    return () => { ro.disconnect(); api.dispose(); };
  }, []);
  return <main class="dock-host" ref={host} aria-label="Workspace" />;
}

export function App() {
  void windowState.value;
  const phase = backend.value?.phase;
  const showOnboarding = phase === 'no-install' || phase === 'needs-config' || phase === 'needs-login' || phase === 'incompatible';
  return (
    <div class="shell" data-zen={zen.value ? '1' : undefined}>
      <TitleBar />
      <Rail />
      <Sidebar />
      <Dock />
      <StatusBar />
      {showOnboarding ? <Onboarding /> : null}
      {popover.value === 'backend' ? <BackendPopover /> : null}
      {popover.value === 'bg' ? <BgPicker /> : null}
      <Palette />
      <DialogHost />
      <Toasts />
    </div>
  );
}
export { VIEW_IDS };
