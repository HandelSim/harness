// Updates view (3.13): the app's own update (bridge.update, state pushed by main) and the harness checkout's
// (bridge.backend.updateGet / job 'check-updates' | 'upgrade'). Phases come from main; nothing is re-derived.
import { useEffect, useState } from 'preact/hooks';
import type { PanelCtx } from '../../shell/dock';
import { updateState, appInfo, offlineReason } from '../../core/app';
import { bridge } from '../../core/bridge';
import { ago } from '../../core/format';
import { Button, Pill } from '../../ui/kit';
import { Icon } from '../../ui/Icon';
import type { HarnessUpdateState } from '../../../shared/ipc';

function useJob(): [string, boolean, (j: 'check-updates' | 'upgrade') => Promise<void>] {
  const [out, setOut] = useState('');
  const [busy, setBusy] = useState(false);
  const run = async (j: 'check-updates' | 'upgrade') => {
    setOut(''); setBusy(true);
    try {
      const { id } = await bridge.harness.job(j);
      const off1 = bridge.on('job:output', (o) => { if (o.id === id) setOut((s) => (s + o.chunk).slice(-20000)); });
      await new Promise<void>((res) => { const off2 = bridge.on('job:exit', (x) => { if (x.id === id) { off1(); off2(); res(); } }); });
    } catch (e) { setOut(String((e as Error)?.message || e)); }
    setBusy(false);
  };
  return [out, busy, run];
}

export default function UpdatesView({ ctx }: { ctx: PanelCtx }) {
  const u = updateState.value;
  const [h, setH] = useState<HarnessUpdateState | null>(null);
  const [out, busy, runJob] = useJob();
  const loadH = () => bridge.harness.updateGet().then(setH, () => setH(null));
  useEffect(() => { ctx.setTitle('Updates'); void bridge.update.get().then((s) => { updateState.value = s; }).catch(() => {}); void loadH(); }, []);
  useEffect(() => { if (!busy) void loadH(); }, [busy]);
  return (
    <div class="view updates-view" data-testid="view-updates">
      <div class="view-head toolbar"><Icon name="updates" /><strong>Updates</strong></div>
      <div class="view-body" style={{ overflow: 'auto', padding: 'var(--sp-5) var(--sp-6)', display: 'flex', flexDirection: 'column', gap: 'var(--sp-5)', maxWidth: '820px' }}>
        <section class="card" style={{ padding: 'var(--sp-4) var(--sp-5)' }} data-testid="update-app">
          <div class="row"><strong>Harness Desktop</strong><span class="meta mono">{u?.current || appInfo.value?.version || ''}</span><span class="spacer" />
            {u ? <Pill tone={u.phase === 'ready' || u.phase === 'available' ? 'accent' : u.phase === 'error' ? 'err' : 'default'}>{u.phase === 'idle' ? 'up to date' : u.phase}</Pill> : null}</div>
          {u?.phase === 'unsupported' ? <p class="meta">{u.reason || 'This build does not update itself.'}{u.releasePage ? ' ' : ''}{u.releasePage ? <a href="#" onClick={(e) => { e.preventDefault(); void bridge.shell.openExternal(u.releasePage); }}>Release page</a> : null}</p> : null}
          {u?.phase === 'error' ? <p class="meta">{u.reason}</p> : null}
          {u?.next ? <p>Version <span class="mono">{u.next}</span> is available.</p> : null}
          {u?.phase === 'downloading' ? <div class="progress" role="progressbar" aria-valuenow={Math.round(u.percent)} aria-valuemin={0} aria-valuemax={100}><span style={{ transform: `scaleX(${Math.max(0, Math.min(1, u.percent / 100))})` }} /></div> : null}
          {u?.notes ? <pre class="pre small" style={{ maxHeight: '200px', overflow: 'auto' }}>{u.notes}</pre> : null}
          <div class="row" style={{ marginTop: 'var(--sp-3)' }}>
            <Button size="sm" icon="refresh" disabled={!u || u.phase === 'unsupported' || u.phase === 'checking' || u.phase === 'downloading'} onClick={() => bridge.update.check().then((s) => { updateState.value = s; })}>Check now</Button>
            {u?.phase === 'available' ? <Button size="sm" variant="primary" onClick={() => bridge.update.download().then((s) => { updateState.value = s; })}>Download</Button> : null}
            {u?.phase === 'ready' ? <Button size="sm" variant="primary" testid="update-install" onClick={() => bridge.update.install()}>Restart to update</Button> : null}
            <span class="spacer" />{u?.checkedAt ? <span class="meta">checked {ago(u.checkedAt)}</span> : null}
          </div>
        </section>
        <section class="card" style={{ padding: 'var(--sp-4) var(--sp-5)' }} data-testid="update-harness">
          <div class="row"><strong>Harness</strong>{h ? <span class="meta mono">{h.branch} · {h.local}</span> : null}<span class="spacer" />
            {h ? <Pill tone={h.available ? 'accent' : h.available === false ? 'ok' : 'default'}>{h.available ? 'update available' : h.available === false ? 'up to date' : 'not checked'}</Pill> : null}</div>
          {h?.text ? <p class="meta" style={{ whiteSpace: 'pre-wrap' }}>{h.text}</p> : null}
          <div class="row" style={{ marginTop: 'var(--sp-3)' }}>
            <Button size="sm" icon="refresh" disabled={busy} onClick={() => runJob('check-updates')}>Check</Button>
            <Button size="sm" variant="primary" disabled={busy || !h?.available || !!offlineReason.value} title={offlineReason.value || undefined}
              onClick={async () => { if (window.confirm('Upgrade the harness now? The backend restarts.')) await runJob('upgrade'); }}>Upgrade</Button>
            <span class="spacer" />{h?.checkedAt ? <span class="meta">checked {ago(h.checkedAt)}</span> : null}
          </div>
          {out ? <pre class="pre small" style={{ maxHeight: '260px', overflow: 'auto', marginTop: 'var(--sp-3)' }}>{out}</pre> : null}
        </section>
      </div>
    </div>
  );
}
