// Web panel (3.16): the serve page in a locked-down, themed <webview>. Guarantees parity for every page that is
// not native yet: unknown/unavailable views route here. Navigation stays on the serve origin (main enforces).
import { useEffect, useRef, useState } from 'preact/hooks';
import type { PanelCtx } from '../../shell/dock';
import { bridge } from '../../core/bridge';
import { backend } from '../../core/app';
import { Button } from '../../ui/kit';
import { Icon } from '../../ui/Icon';
import { VIEW_TITLES, type ViewId } from '../../shell/panels';

type WebviewEl = HTMLElement & {
  src: string; reload(): void; goBack(): void; goForward(): void; canGoBack(): boolean; canGoForward(): boolean;
  getURL(): string; getTitle(): string; findInPage?(t: string): void; stopFindInPage?(a: string): void;
};

/** Guest CSS so serve pages pick up the app's colours (sent once per theme change; main injects it). */
function guestCss(): string {
  const cs = getComputedStyle(document.documentElement);
  const v = (n: string) => cs.getPropertyValue(n).trim();
  return `:root{color-scheme:${document.documentElement.dataset.theme === 'light' ? 'light' : 'dark'}}
html,body{background:${v('--surface-0')} !important;color:${v('--text-1')};font-family:${v('--font-ui')}}
a{color:${v('--accent-11')}} ::selection{background:${v('--accent-4')}}
::-webkit-scrollbar{width:10px;height:10px}::-webkit-scrollbar-thumb{background:${v('--border-strong')};border-radius:8px;border:2px solid transparent;background-clip:padding-box}`;
}

export default function WebPanel({ ctx }: { ctx: PanelCtx }) {
  const path = String(ctx.params.value.path ?? '/');
  const fallbackFor = ctx.params.value.fallbackFor as ViewId | undefined;
  const host = useRef<HTMLDivElement>(null);
  const wv = useRef<WebviewEl | null>(null);
  const [src, setSrc] = useState<{ url: string; partition: string } | null>(null);
  const [err, setErr] = useState('');
  const [loading, setLoading] = useState(true);
  const [addr, setAddr] = useState(path);
  const up = backend.value?.phase === 'up';
  useEffect(() => {
    ctx.setTitle(fallbackFor ? `${VIEW_TITLES[fallbackFor]} (web)` : path);
  }, []);
  useEffect(() => {
    if (!up) return;
    let alive = true;
    bridge.web.url('serve', path).then((r) => {
      if (!alive) return;
      if (r && r.ok) { setSrc({ url: r.url, partition: r.partition }); setErr(''); } else setErr('The serve page is not available.');
    }, (e) => alive && setErr(String(e?.message || e)));
    void bridge.web.theme(guestCss()).catch(() => {});
    return () => { alive = false; };
  }, [up, path]);
  useEffect(() => {
    if (!src || !host.current) return;
    const el = document.createElement('webview') as WebviewEl;
    el.setAttribute('partition', src.partition);
    el.setAttribute('src', src.url);
    el.setAttribute('webpreferences', 'contextIsolation=yes,sandbox=yes,javascript=yes');
    el.className = 'webview';
    el.addEventListener('did-start-loading', () => setLoading(true));
    el.addEventListener('did-stop-loading', () => {
      setLoading(false);
      try {
        const u = new URL(el.getURL());
        setAddr(u.pathname + u.search);
        const t = el.getTitle();
        if (t && !fallbackFor) ctx.setTitle(t.slice(0, 40));
        if (!fallbackFor) ctx.update({ path: u.pathname + u.search });
      } catch { /* ignore */ }
    });
    el.addEventListener('did-fail-load', (e: Event) => {
      const ev = e as Event & { errorCode?: number; errorDescription?: string; isMainFrame?: boolean };
      if (ev.isMainFrame && ev.errorCode !== -3) setErr(ev.errorDescription || 'Load failed');
    });
    host.current.replaceChildren(el);
    wv.current = el;
    return () => { el.remove(); wv.current = null; };
  }, [src?.url]);
  const go = (fn: (w: WebviewEl) => void) => { const w = wv.current; if (w) try { fn(w); } catch { /* not attached yet */ } };
  return (
    <div class="view web-view" data-testid={fallbackFor ? `view-${fallbackFor}` : undefined}>
      <div class="web-bar">
        <button class="btn btn--sm btn--ghost btn--icon" aria-label="Back" onClick={() => go((w) => w.canGoBack() && w.goBack())}><Icon name="back" size={14} /></button>
        <button class="btn btn--sm btn--ghost btn--icon" aria-label="Forward" onClick={() => go((w) => w.canGoForward() && w.goForward())}><Icon name="forward" size={14} /></button>
        <button class="btn btn--sm btn--ghost btn--icon" aria-label="Reload" onClick={() => go((w) => w.reload())}>{loading ? <span class="spin" /> : <Icon name="refresh" size={14} />}</button>
        <div class="web-addr mono small ellipsis" title={addr}>{addr}</div>
        {fallbackFor ? <span class="pill pill--info">web version</span> : null}
        <button class="btn btn--sm btn--ghost btn--icon" aria-label="Open in browser" title="Open in browser"
          onClick={() => { if (src) void bridge.shell.openExternal(new URL(addr, src.url).toString()); }}><Icon name="external" size={14} /></button>
      </div>
      <div class="web-host" ref={host}>
        {!up ? <div class="empty"><p>Waiting for the backend…</p></div> : null}
      </div>
      {err ? (
        <div class="error-panel" data-testid="error-panel" style={{ position: 'absolute', left: '16px', right: '16px', bottom: '16px' }}>
          <div class="msg">{err}</div>
          <div class="row"><Button size="sm" icon="refresh" onClick={() => { setErr(''); go((w) => w.reload()); }}>Retry</Button></div>
        </div>
      ) : null}
    </div>
  );
}
