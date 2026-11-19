// Backend popover (W1/W2, 3.15): phase, URL, ownership, restart/stop/retry, log tail, folder.
import { useEffect, useRef } from 'preact/hooks';
import { backend } from '../core/app';
import { bridge } from '../core/bridge';
import { popover } from './ui';
import { Button, Pill } from '../ui/kit';
import { ago } from '../core/format';
import type { BackendAction } from '../../shared/ipc';
import { toast } from '../core/toast';

export async function backendAction(a: BackendAction): Promise<void> {
  try {
    const s = await bridge.backend.action(a);
    if (s && 'phase' in s) backend.value = s;
  } catch (e) { toast({ kind: 'err', title: 'Backend action failed', body: String((e as Error)?.message || e) }); }
}

export function BackendPopover() {
  const b = backend.value;
  const el = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const off = (e: MouseEvent) => {
      const t = e.target as Node;
      if (el.current && !el.current.contains(t) && !(t as HTMLElement).closest?.('[data-testid="status-backend"],[data-testid="backend-chip"]')) popover.value = null;
    };
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') popover.value = null; };
    window.addEventListener('pointerdown', off, true); window.addEventListener('keydown', esc);
    return () => { window.removeEventListener('pointerdown', off, true); window.removeEventListener('keydown', esc); };
  }, []);
  const tone = b?.phase === 'up' ? 'ok' : b?.phase === 'down' || b?.phase === 'incompatible' ? 'err' : 'warn';
  return (
    <div ref={el} class="popover glass glass--overlay col" data-testid="backend-popover" role="dialog" aria-label="Backend"
      style={{ left: '12px', bottom: 'calc(var(--status-h) + 8px)', gap: 'var(--sp-3)', width: '380px' }}>
      <div class="row"><strong>Backend</strong><Pill tone={tone} dot={b?.phase === 'up' ? 'ok' : undefined}>{b?.phase ?? 'locating'}</Pill><span class="spacer" />
        {b?.since ? <span class="meta">{ago(b.since)}</span> : null}</div>
      <dl class="kv">
        <dt>URL</dt><dd class="mono small">{b?.url ?? '—'}</dd>
        <dt>Install</dt><dd class="mono small ellipsis" title={b?.root ?? ''}>{b?.root ?? 'not found'}</dd>
        <dt>Process</dt><dd>{b?.owned ? `started by this app${b.pid ? ` (pid ${b.pid})` : ''}` : b?.phase === 'up' ? 'attached to a running serve' : '—'}</dd>
        {b?.apiVersion != null ? <><dt>API</dt><dd>v{b.apiVersion}</dd></> : null}
      </dl>
      {b?.problem ? <div class="notice notice--warn"><strong>{b.problem.code}</strong> {b.problem.message}</div> : null}
      {b?.logTail?.length ? <pre class="pre small" style={{ maxHeight: '160px', overflow: 'auto' }}>{b.logTail.slice(-30).join('\n')}</pre> : null}
      <div class="row" style={{ flexWrap: 'wrap', gap: 'var(--sp-2)' }}>
        {b?.phase === 'up' ? <Button size="sm" icon="refresh" onClick={() => backendAction({ action: 'restart' })} disabled={!b.owned} title={b.owned ? '' : 'attached: restart it where it was started'}>Restart</Button> : null}
        {b?.phase === 'up' && b.owned ? <Button size="sm" variant="danger" icon="stop" onClick={() => backendAction({ action: 'stop' })}>Stop</Button> : null}
        {b?.phase === 'down' || b?.phase === 'reconnecting' ? <Button size="sm" variant="primary" icon="play" onClick={() => backendAction({ action: 'start' })}>Start</Button> : null}
        {b?.phase !== 'up' ? <Button size="sm" icon="retry" onClick={() => backendAction({ action: 'retry' })}>Retry</Button> : null}
        <Button size="sm" variant="ghost" icon="folder" onClick={async () => {
          const p = await bridge.shell.pickFolder(b?.root ?? undefined);
          if (p) await backendAction({ action: 'set-root', root: p });
        }}>Change folder…</Button>
        {b?.root ? <Button size="sm" variant="ghost" icon="external" onClick={() => bridge.shell.showItem(b.root!)}>Show</Button> : null}
      </div>
    </div>
  );
}
