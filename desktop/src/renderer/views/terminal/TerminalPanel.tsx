// Terminal panel (W2-W4, W18, 3.7): xterm 6 attached to a pty from the pty-host. Attaches to a live pty after a
// layout restore (replay ring), otherwise spawns; flow control acks consumed chars; WebGL only while visible (cap 8).
import { useEffect, useRef, useState } from 'preact/hooks';
import { effect } from '@preact/signals';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import { WebglAddon } from '@xterm/addon-webgl';
import { SearchAddon } from '@xterm/addon-search';
import { WebLinksAddon } from '@xterm/addon-web-links';
import { Unicode11Addon } from '@xterm/addon-unicode11';
import type { PtySpec } from '../../../shared/ipc';
import type { PanelCtx } from '../../shell/dock';
import { openPanel, paramClaimed } from '../../shell/dock';
import { findRequest, dialog } from '../../shell/ui';
import { commandsFor } from '../../shell/commands';
import { settings } from '../../core/app';
import { bridge, on } from '../../core/bridge';
import { Button } from '../../ui/kit';
import { Icon } from '../../ui/Icon';
import { resolveXtermTheme, searchDecorations } from './xterm-theme';
import { specTitle, pasteLines, pasteNeedsConfirm, WebglSlots, PendingBuffer, isLaunchFailure, exitText, canTryHost, KIND_LABEL, type ExitInfo } from './model';
import { openNewSessionWith } from '../session/NewSessionDialog';

// ---- module-level pty event dispatch: one subscription, routed by id; output that races spawn() is buffered ----
interface Sink { data(d: string): void; exit(e: ExitInfo): void }
const sinks = new Map<string, Sink>();
const pending = new PendingBuffer();
const pendingExit = new Map<string, ExitInfo>();
const webgl = new WebglSlots(8);
let wired = false;
function wire(): void {
  if (wired) return;
  wired = true;
  on('pty:data', ({ id, data }) => { const s = sinks.get(id); if (s) s.data(data); else pending.push(id, data, Date.now()); });
  on('pty:exit', ({ id, code, signal }) => {
    const s = sinks.get(id);
    if (s) { s.exit({ code, signal }); return; }
    pendingExit.set(id, { code, signal });
    if (pendingExit.size > 64) pendingExit.delete(pendingExit.keys().next().value as string);
  });
}
/** After a renderer reload the host may still count output the dead renderer never acked; acking past HIGH resets it. */
const ACK_RESET = 4 * 1024 * 1024;

type Phase = 'starting' | 'running' | 'exited' | 'error';
const MAC = typeof navigator !== 'undefined' && /mac/i.test(navigator.platform);

export default function TerminalPanel({ ctx }: { ctx: PanelCtx }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const api = useRef<{ restart(): void; clear(): void; find(q: string, back?: boolean, typing?: boolean): void; closeFind(): void; paste(t: string): void; focus(): void } | null>(null);
  const [ptyId, setPtyId] = useState<string | null>(typeof ctx.params.peek().ptyId === 'string' ? (ctx.params.peek().ptyId as string) : null);
  const [phase, setPhase] = useState<Phase>('starting');
  const [exit, setExit] = useState<{ e: ExitInfo; failed: boolean } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [findOpen, setFindOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<{ i: number; n: number } | null>(null);
  const [pasteAsk, setPasteAsk] = useState<string | null>(null);
  const spec = ctx.params.peek().spec as PtySpec;

  useEffect(() => {
    const host = hostRef.current!;
    wire();
    let disposed = false;
    let id: string | null = null;
    let alive = false;
    let startedAt = Date.now();
    let hasGl: WebglAddon | null = null;
    const subs: (() => void)[] = [];
    const t = settings.peek()?.terminal;

    const term = new Terminal({
      allowProposedApi: true,
      allowTransparency: false,
      minimumContrastRatio: 4.5,
      cursorStyle: 'bar',
      cursorInactiveStyle: 'outline',
      cursorBlink: t?.cursorBlink ?? true,
      scrollback: t?.scrollback ?? 5000,
      customGlyphs: true,
      fontFamily: t?.fontFamily || '"JetBrains Mono Variable", monospace',
      fontSize: t?.fontSize ?? 13,
      lineHeight: t?.lineHeight ?? 1.2,
      macOptionIsMeta: true,
      rightClickSelectsWord: MAC,
      theme: resolveXtermTheme(),
    });
    const fit = new FitAddon();
    const search = new SearchAddon();
    term.loadAddon(fit);
    term.loadAddon(search);
    term.loadAddon(new WebLinksAddon((ev, uri) => { ev.preventDefault(); void bridge.shell.openExternal(uri); }));
    term.loadAddon(new Unicode11Addon());
    term.unicode.activeVersion = '11';
    const resultsSub = search.onDidChangeResults((r) => setResults(r.resultCount ? { i: r.resultIndex, n: r.resultCount } : { i: -1, n: 0 }));
    subs.push(() => resultsSub.dispose());

    // W18: keys the app owns never reach the pty; Ctrl+Shift+C/V are copy/paste on Linux/Windows.
    term.attachCustomKeyEventHandler((e) => {
      if (e.type !== 'keydown') return true;
      if (!MAC && e.ctrlKey && e.shiftKey && !e.altKey && (e.code === 'KeyC' || e.code === 'KeyV')) {
        if (e.code === 'KeyC') { e.preventDefault(); copySelection(); }
        return false; // KeyV: the browser's native paste fires a 'paste' event handled below
      }
      return commandsFor(e).length ? false : true;
    });
    const copySelection = () => {
      const s = term.getSelection();
      if (!s) return;
      navigator.clipboard?.writeText(s).catch(() => document.execCommand('copy'));
    };
    // All paste routes (keyboard, context menu, middle click) come through here so the multi-line guard applies.
    const onPaste = (e: ClipboardEvent) => {
      const text = e.clipboardData?.getData('text/plain') ?? '';
      e.preventDefault();
      e.stopPropagation();
      if (!text || !alive) return;
      if (pasteNeedsConfirm(text, settings.peek()?.terminal.confirmPaste ?? true, false)) setPasteAsk(text);
      else term.paste(text);
    };
    host.addEventListener('paste', onPaste, true);

    const doFit = () => {
      if (disposed || !term.element || !ctx.visible.peek() || !host.offsetParent) return;
      const d = fit.proposeDimensions();
      if (d && d.cols > 1 && d.rows > 1 && (d.cols !== term.cols || d.rows !== term.rows)) fit.fit();
    };
    term.onResize(({ cols, rows }) => { if (id && alive) bridge.pty.resize(id, cols, rows); });
    term.onData((d) => { if (id && alive) bridge.pty.write(id, d); });

    let raf = 0;
    const ro = new ResizeObserver(() => { cancelAnimationFrame(raf); raf = requestAnimationFrame(doFit); });
    ro.observe(host);

    // theme follows <html data-theme/data-accent>
    let themeKey = '';
    const applyTheme = () => { const th = resolveXtermTheme(); const k = JSON.stringify(th); if (k !== themeKey) { themeKey = k; term.options.theme = th; } };
    const mo = new MutationObserver(applyTheme);
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'data-accent', 'style', 'class'] });

    // live settings
    subs.push(effect(() => {
      const s = settings.value?.terminal;
      if (!s) return;
      const o = term.options;
      const ff = s.fontFamily || '"JetBrains Mono Variable", monospace';
      if (o.fontFamily !== ff) o.fontFamily = ff;
      if (o.fontSize !== s.fontSize) o.fontSize = s.fontSize;
      if (o.lineHeight !== s.lineHeight) o.lineHeight = s.lineHeight;
      if (o.cursorBlink !== s.cursorBlink) o.cursorBlink = s.cursorBlink;
      if (o.scrollback !== s.scrollback) o.scrollback = s.scrollback;
      requestAnimationFrame(doFit);
      syncGl();
    }));

    // WebGL only while visible, capped across all terminals; DOM renderer otherwise.
    function syncGl() {
      if (!term.element) return;
      const want = ctx.visible.peek() && (settings.peek()?.terminal.webgl ?? true);
      if (want && !hasGl && webgl.acquire(ctx.id)) {
        try {
          const gl = new WebglAddon();
          gl.onContextLoss(() => { if (hasGl === gl) { hasGl = null; webgl.release(ctx.id); } gl.dispose(); });
          term.loadAddon(gl);
          hasGl = gl;
        } catch { webgl.release(ctx.id); }
      } else if (!want && hasGl) {
        const gl = hasGl; hasGl = null; webgl.release(ctx.id);
        try { gl.dispose(); } catch { /* already gone */ }
      }
    }
    subs.push(effect(() => { const v = ctx.visible.value; syncGl(); if (v) requestAnimationFrame(doFit); }));

    // focus when the tab becomes active by the user, never stealing it from an input elsewhere or a dialog
    subs.push(effect(() => {
      if (!ctx.active.value) return;
      requestAnimationFrame(() => {
        if (disposed || dialog.peek()) return;
        const ae = document.activeElement as HTMLElement | null;
        if (!ae || ae === document.body || host.contains(ae) || ae.closest('.dv-tab, .dv-tabs-container')) term.focus();
      });
    }));

    // Mod+F while this panel is active opens the find bar
    let seenFind = findRequest.peek();
    subs.push(effect(() => {
      const n = findRequest.value;
      if (n === seenFind) return;
      seenFind = n;
      if (!ctx.active.peek()) return;
      const sel = term.getSelection();
      if (sel && !sel.includes('\n')) setQuery(sel);
      setFindOpen(true);
      requestAnimationFrame(() => { searchRef.current?.focus(); searchRef.current?.select(); });
    }));

    function runFind(q: string, back = false, typing = false) {
      if (!q) { search.clearDecorations(); setResults(null); return; }
      const opts = { incremental: typing, decorations: searchDecorations(document.documentElement.dataset.theme === 'light') };
      if (back) search.findPrevious(q, opts); else search.findNext(q, opts);
    }

    // ---- pty lifecycle ----
    const sinkFor = (pid: string, ready: { v: boolean }): Sink => ({
      data(d) {
        if (!ready.v) return; // pre-attach: already in the replay snapshot
        if (phaseNow === 'starting') toRunning();
        term.write(d, () => bridge.pty.ack(pid, d.length));
      },
      exit(e) { if (ready.v) onExit(e); }, // pre-attach exits are in the reply's info
    });
    let phaseNow: Phase = 'starting';
    const setP = (p: Phase) => { phaseNow = p; setPhase(p); };
    const toRunning = () => { setP('running'); ctx.setTabState('running'); };
    function onExit(e: ExitInfo) {
      if (!alive && phaseNow === 'exited') return;
      alive = false;
      const failed = isLaunchFailure(startedAt, Date.now(), e);
      setExit({ e, failed });
      setP('exited');
      ctx.setTabState(failed ? 'error' : 'ended');
    }
    function bind(pid: string, ready: { v: boolean }) {
      if (id) sinks.delete(id);
      id = pid;
      sinks.set(pid, sinkFor(pid, ready));
      setPtyId(pid);
    }

    async function attach(pid: string): Promise<boolean> {
      if (sinks.has(pid)) return false; // another panel (e.g. a split copy) owns it: start a new session instead
      const ready = { v: false };
      bind(pid, ready);
      const a = await bridge.pty.attach(pid).catch(() => null);
      if (disposed) return true;
      if (!a) { sinks.delete(pid); id = null; return false; }
      pending.take(pid);
      ready.v = true;
      startedAt = a.info.startedAt;
      if (a.replay) term.write(a.replay);
      bridge.pty.ack(pid, ACK_RESET);
      if (a.info.exited) { alive = true; onExit(a.info.exited); }
      else {
        alive = true; toRunning();
        if (term.cols && term.rows) bridge.pty.resize(pid, term.cols, term.rows);
      }
      return true;
    }

    let gen = 0;
    async function spawn(s: PtySpec) {
      const my = ++gen;
      setP('starting'); setError(null); setExit(null);
      ctx.setTabState('starting');
      doFit();
      startedAt = Date.now();
      const r = await bridge.pty.spawn(s, term.cols || 80, term.rows || 24).catch((e: unknown) => ({ ok: false as const, error: { code: 'ipc', message: String((e as Error)?.message ?? e) } }));
      if (disposed || my !== gen) { if (r.ok) bridge.pty.kill(r.id); return; }
      if (!r.ok) { setError(r.error.message || 'Could not start'); setP('error'); ctx.setTabState('error'); return; }
      alive = true;
      bind(r.id, { v: true });
      ctx.update({ ptyId: r.id });
      for (const d of pending.take(r.id)) { if (phaseNow === 'starting') toRunning(); term.write(d, () => bridge.pty.ack(r.id, d.length)); }
      const ex = pendingExit.get(r.id);
      if (ex) { pendingExit.delete(r.id); onExit(ex); }
      if (term.cols && term.rows) bridge.pty.resize(r.id, term.cols, term.rows);
    }

    function restart() {
      if (id && alive) bridge.pty.kill(id);
      if (id) sinks.delete(id);
      alive = false; id = null;
      term.reset();
      void spawn(spec);
    }

    ctx.setTitle(specTitle(spec));
    ctx.beforeClose(async () => {
      if (!id || !alive) return true;
      const ask = settings.peek()?.terminal.confirmClose ?? true;
      const what = spec.kind === 'agent' || spec.kind === 'host' ? 'the agent' : `the ${KIND_LABEL[spec.kind]}`;
      if (ask && !window.confirm(`Stop ${what} and close?`)) return false;
      bridge.pty.kill(id);
      alive = false;
      return true;
    });
    // Disposed without beforeClose (layout undo/reset, restore failure): kill the pty unless the same operation
    // recreated a panel that re-attaches to it; otherwise it would pause at the unacked cap and block the agent (H3).
    ctx.onDispose(() => { if (id && alive && !paramClaimed('ptyId', id)) { bridge.pty.kill(id); alive = false; } });

    api.current = {
      restart,
      clear: () => { term.clear(); term.focus(); },
      find: (q, back, typing) => runFind(q, back, typing),
      closeFind: () => { search.clearDecorations(); setResults(null); setFindOpen(false); term.focus(); },
      paste: (text) => { term.paste(text); term.focus(); },
      focus: () => term.focus(),
    };

    (async () => {
      // fonts are local (fontsource); wait briefly so the first measurement uses the real face
      try { await Promise.race([document.fonts.load(`${term.options.fontSize}px "JetBrains Mono Variable"`), new Promise((r) => setTimeout(r, 600))]); } catch { /* ignore */ }
      if (disposed) return;
      term.open(host);
      applyTheme();
      doFit();
      syncGl();
      const saved = ctx.params.peek().ptyId;
      if (typeof saved === 'string' && await attach(saved)) return;
      if (disposed) return;
      const restore = settings.peek()?.sessions.restore;
      const s: PtySpec = typeof saved === 'string' && restore === 'tabs-and-resume' && spec.kind === 'host' && !spec.resume ? { ...spec, resume: 'last' } : spec;
      await spawn(s);
    })();

    return () => {
      disposed = true;
      cancelAnimationFrame(raf);
      ro.disconnect();
      mo.disconnect();
      host.removeEventListener('paste', onPaste, true);
      for (const u of subs) { try { u(); } catch { /* ignore */ } }
      if (id) sinks.delete(id);
      if (hasGl) { webgl.release(ctx.id); hasGl = null; }
      api.current = null;
      term.dispose();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (pasteAsk === null) return;
    const f = requestAnimationFrame(() => hostRef.current?.parentElement?.querySelector<HTMLButtonElement>('[data-testid="term-paste-ok"]')?.focus());
    return () => cancelAnimationFrame(f);
  }, [pasteAsk]);

  const onFindKey = (e: KeyboardEvent) => {
    if (e.key === 'Enter') { e.preventDefault(); api.current?.find(query, e.shiftKey); }
    else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); api.current?.closeFind(); }
  };
  const confirmPaste = (yes: boolean) => {
    const t = pasteAsk; setPasteAsk(null);
    if (yes && t) api.current?.paste(t); else api.current?.focus();
  };
  const splitNew = () => openPanel('terminal', null, { split: 'right', title: specTitle(spec), params: { spec } });

  return (
    <div class="view term-panel" data-testid={`terminal-${ptyId ?? 'starting'}`} data-phase={phase}>
      <div class="term-host" ref={hostRef} />

      <div class="term-tools" role="toolbar" aria-label="Terminal">
        <Button variant="ghost" size="sm" icon="search" iconOnly title="Find (Mod+F)" testid="term-find" onClick={() => { setFindOpen(true); requestAnimationFrame(() => searchRef.current?.focus()); }} />
        <Button variant="ghost" size="sm" icon="trash" iconOnly title="Clear scrollback" testid="term-clear" onClick={() => api.current?.clear()} />
        <Button variant="ghost" size="sm" icon="split" iconOnly title="New session to the right" testid="term-split" onClick={splitNew} />
        <Button variant="ghost" size="sm" icon="retry" iconOnly title="Restart" testid="term-restart" onClick={() => api.current?.restart()} />
      </div>

      {findOpen ? (
        <div class="term-find glass" role="search">
          <Icon name="search" size={14} />
          <input ref={searchRef} class="term-find__input" value={query} placeholder="Find" aria-label="Find in terminal" data-testid="term-find-input"
            onInput={(e) => { const q = (e.currentTarget as HTMLInputElement).value; setQuery(q); api.current?.find(q, false, true); }} onKeyDown={onFindKey} />
          <span class="term-find__count tnum">{results ? (results.n ? `${results.i + 1}/${results.n}` : 'No results') : ''}</span>
          <Button variant="ghost" size="sm" icon="back" iconOnly title="Previous (Shift+Enter)" onClick={() => api.current?.find(query, true)} />
          <Button variant="ghost" size="sm" icon="forward" iconOnly title="Next (Enter)" onClick={() => api.current?.find(query)} />
          <Button variant="ghost" size="sm" icon="x" iconOnly title="Close (Esc)" onClick={() => api.current?.closeFind()} />
        </div>
      ) : null}

      {phase === 'starting' ? <div class="term-starting faint small"><span class="spin" aria-hidden="true" /> Starting {KIND_LABEL[spec.kind]} in {spec.cwd}</div> : null}

      {pasteAsk !== null ? (
        <div class="term-bar term-bar--ask" role="alertdialog" aria-label="Confirm paste" data-testid="term-paste-confirm"
          onKeyDown={(e) => { if (e.key === 'Escape') { e.stopPropagation(); confirmPaste(false); } }}>
          <Icon name="alert" size={14} />
          <span class="grow">Paste {pasteLines(pasteAsk)} {pasteLines(pasteAsk) === 1 ? 'line' : 'lines'}?</span>
          <Button size="sm" onClick={() => confirmPaste(false)}>Cancel</Button>
          <Button size="sm" variant="primary" onClick={() => confirmPaste(true)} testid="term-paste-ok">Paste</Button>
        </div>
      ) : null}

      {phase === 'exited' && exit ? (
        <div class={`term-bar ${exit.failed ? 'term-bar--err' : ''}`} role="status" data-testid="term-exit">
          <Icon name={exit.failed ? 'alert' : 'info'} size={14} />
          <span class="grow">{exitText(exit.e)}</span>
          {exit.failed ? <Button size="sm" icon="edit" onClick={() => openNewSessionWith(spec)}>Edit options</Button> : null}
          {exit.failed && canTryHost(spec) ? <Button size="sm" onClick={() => openNewSessionWith({ ...spec, kind: 'host' })}>Try host mode</Button> : null}
          <Button size="sm" variant="primary" icon="retry" onClick={() => api.current?.restart()} testid="term-restart-bar">Restart</Button>
        </div>
      ) : null}

      {phase === 'error' ? (
        <div class="term-bar term-bar--err" role="alert" data-testid="term-error">
          <Icon name="alert" size={14} />
          <span class="grow">Could not start: {error}</span>
          <Button size="sm" icon="edit" onClick={() => openNewSessionWith(spec)}>Edit options</Button>
          <Button size="sm" variant="primary" icon="retry" onClick={() => api.current?.restart()}>Retry</Button>
        </div>
      ) : null}
    </div>
  );
}
