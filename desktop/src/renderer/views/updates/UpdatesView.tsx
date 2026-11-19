// Updates view (3.13): the app's own update (bridge.update, state pushed by main) and the harness checkout's
// (bridge.backend.updateGet / job 'check-updates' | 'upgrade'). Phases come from main; nothing is re-derived.
import { useEffect, useState } from 'preact/hooks';
import type { PanelCtx } from '../../shell/dock';
import { updateState, appInfo, offlineReason, settings, patchSettings } from '../../core/app';
import { bridge, api } from '../../core/bridge';
import { ago } from '../../core/format';
import { Button, Pill } from '../../ui/kit';
import { Icon } from '../../ui/Icon';
import { confirmDialog } from '../ticket/parts';
import type { AppBusy, HarnessUpdateState, JobInfo } from '../../../shared/ipc';
import { restartConsequences, upgradeConsequences } from './consequences';
import type { Queue } from '../../../shared/api';

type Job = 'check-updates' | 'upgrade';

/** What a restart / upgrade would interrupt (main's app:busy; counted here when main lacks it). */
export async function busyNow(): Promise<AppBusy | null> {
  try { const b = await bridge.app.busy(); if (b && typeof b.terminals === 'number') return b; } catch { /* main without app:busy */ }
  try {
    const [ptys, q] = await Promise.all([bridge.pty.list().catch(() => []), bridge.api.get<Queue>(api('/queue')).catch(() => null)]);
    return { terminals: (Array.isArray(ptys) ? ptys : []).filter((p) => !p.exited).length,
      queueRunning: !!(q && q.ok && q.data?.runner?.running), ownedServe: false,
      onQuit: settings.value?.backend.onQuit ?? 'keep-if-busy', jobs: [] };
  } catch { return null; }
}

const list = (xs: string[]) => <ul class="small" style={{ margin: 0, paddingLeft: '1.2em' }}>{xs.map((x) => <li key={x}>{x}</li>)}</ul>;

function useJob(): [string, boolean, (j: Job) => Promise<void>] {
  const [out, setOut] = useState('');
  const [busy, setBusy] = useState(false);
  const jobsNow = () => bridge.harness.jobs().then((js) => (Array.isArray(js) ? js : []), () => [] as JobInfo[]);
  /** Stream a job's output until it exits; `tail` is what it printed before we attached. */
  const follow = async (id: string, tail: string) => {
    setOut(tail.slice(-20000)); setBusy(true);
    await new Promise<void>((res) => {
      let done = false;
      const fin = (code: number | null) => { if (done) return; done = true; off1(); off2(); if (code) setOut((s) => `${s}\n[exit ${code}]`); res(); };
      const off1 = bridge.on('job:output', (o) => { if (o.id === id) setOut((s) => (s + o.chunk).slice(-20000)); });
      const off2 = bridge.on('job:exit', (x) => { if (x.id === id) fin(x.code); });
      // it may have ended before we subscribed
      void jobsNow().then((js) => { const j = js.find((x) => x.id === id); if (j && !j.running) { setOut(j.output.slice(-20000)); fin(j.code); } });
    });
    setBusy(false);
  };
  // M3: re-attach to a job main is still running (this panel was closed and reopened, or is a second copy in a split)
  useEffect(() => {
    void jobsNow().then((js) => { const r = js.find((j) => j.running && (j.job === 'upgrade' || j.job === 'check-updates')); if (r) void follow(r.id, r.output); });
  }, []);
  const run = async (j: Job) => {
    setOut(''); setBusy(true);
    try {
      const before = await jobsNow();
      const { id } = await bridge.harness.job(j);   // main returns the running job's id instead of starting a second one
      const running = before.find((x) => x.id === id && x.running);
      await follow(id, running ? running.output : '');
    } catch (e) { setOut(String((e as Error)?.message || e)); setBusy(false); }
  };
  return [out, busy, run];
}

async function confirmRestart(): Promise<boolean> {
  const b = await busyNow();
  return confirmDialog({ title: 'Restart to update?', body: list(restartConsequences(b)), ok: 'Restart now', danger: !!b && (b.terminals > 0 || b.queueRunning) });
}
async function confirmUpgrade(): Promise<boolean> {
  const b = await busyNow();
  if (b?.jobs.includes('upgrade')) return true;  // already running: job() re-attaches, nothing new starts
  return confirmDialog({ title: 'Upgrade the harness now?', body: list(upgradeConsequences(b)), ok: 'Upgrade', danger: !!b?.queueRunning });
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
            {u?.phase === 'ready' ? <Button size="sm" variant="primary" testid="update-install" onClick={async () => { if (await confirmRestart()) await bridge.update.install(); }}>Restart to update</Button> : null}
            <span class="spacer" />{u?.checkedAt ? <span class="meta">checked {ago(u.checkedAt)}</span> : null}
          </div>
          {settings.value && u?.phase !== 'unsupported' ? (
            <label class="check" style={{ marginTop: 'var(--sp-2)' }} data-testid="update-auto">
              <input type="checkbox" checked={settings.value.updates.auto}
                onChange={(e) => void patchSettings({ updates: { auto: (e.target as HTMLInputElement).checked } })} />
              Check for app updates automatically (on start and every 6 hours)
            </label>
          ) : null}
        </section>
        <section class="card" style={{ padding: 'var(--sp-4) var(--sp-5)' }} data-testid="update-harness">
          <div class="row"><strong>Harness</strong>{h ? <span class="meta mono">{h.branch} · {h.local}</span> : null}<span class="spacer" />
            {h ? <Pill tone={h.available ? 'accent' : h.available === false ? 'ok' : 'default'}>{h.available ? 'update available' : h.available === false ? 'up to date' : 'not checked'}</Pill> : null}</div>
          {h?.text ? <p class="meta" style={{ whiteSpace: 'pre-wrap' }}>{h.text}</p> : null}
          <div class="row" style={{ marginTop: 'var(--sp-3)' }}>
            <Button size="sm" icon="refresh" disabled={busy} onClick={() => runJob('check-updates')}>Check</Button>
            <Button size="sm" variant="primary" disabled={busy || !h?.available || !!offlineReason.value} title={offlineReason.value || undefined}
              testid="harness-upgrade" onClick={async () => { if (await confirmUpgrade()) await runJob('upgrade'); }}>Upgrade</Button>
            <span class="spacer" />{h?.checkedAt ? <span class="meta">checked {ago(h.checkedAt)}</span> : null}
          </div>
          {out ? <pre class="pre small" style={{ maxHeight: '260px', overflow: 'auto', marginTop: 'var(--sp-3)' }}>{out}</pre> : null}
        </section>
      </div>
    </div>
  );
}
