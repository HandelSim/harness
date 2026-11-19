// Debug view: the /debug list (queue debug runs and standalone reports, newest first) beside the selected report
// (/debug/<kind>/<name>, Markdown). Up/Down move the selection; the selection survives a restart (panel params).
import { useEffect, useMemo, useState } from 'preact/hooks';
import { useTopic } from '../../core/app';
import { api, bridge } from '../../core/bridge';
import { ago } from '../../core/format';
import { openPanel, openWeb, type PanelCtx } from '../../shell/dock';
import { AsOf, Button, EmptyState, ErrorPanel, Pill, Seg, Skeleton, TopicBody } from '../../ui/kit';
import { Icon } from '../../ui/Icon';
import { Markdown } from '../../ui/Markdown';
import { VirtualList } from '../queue/virtual';
import { debugId, filterDebug, pickSelection, reportPath, verdictTone, type DebugKind, type DebugRow } from './model';

const ROW_H = 52;

export default function DebugView({ ctx }: { ctx: PanelCtx }) {
  const e = useTopic('debug');
  const [kind, setKind] = useState<DebugKind>('all');
  const [q, setQ] = useState('');
  const [sel, setSelState] = useState<string>(typeof ctx.params.value.sel === 'string' ? ctx.params.value.sel : '');
  useEffect(() => { ctx.setTitle('Debug'); }, []);
  const all = (e?.data.value ?? []) as DebugRow[];
  const rows = useMemo(() => filterDebug(Array.isArray(all) ? all : [], kind, q), [all, kind, q]);
  const cur = pickSelection(rows, sel);
  const idx = rows.findIndex((r) => debugId(r) === cur);
  const setSel = (id: string) => { setSelState(id); ctx.update({ sel: id }); };
  const onKey = (ev: KeyboardEvent) => {
    if (ev.key !== 'ArrowDown' && ev.key !== 'ArrowUp') return;
    ev.preventDefault();
    const n = rows[Math.max(0, Math.min(rows.length - 1, idx + (ev.key === 'ArrowDown' ? 1 : -1)))];
    if (n) setSel(debugId(n));
  };
  const row = idx >= 0 ? rows[idx] : null;
  return (
    <div class="view debug-view" data-testid="view-debug">
      <div class="view-head">
        <Seg label="Kind" value={kind} onChange={setKind} options={[{ value: 'all', label: 'All' }, { value: 'runs', label: 'Runs' }, { value: 'reports', label: 'Reports' }]} />
        <label class="search-field"><Icon name="search" size={13} />
          <input class="input input--bare" placeholder="Filter" aria-label="Filter debug reports" data-testid="debug-filter" value={q}
            onInput={(ev) => setQ((ev.target as HTMLInputElement).value)} onKeyDown={onKey} />
        </label>
        <span class="grow" />
        <AsOf e={e} />
        <Button size="sm" variant="ghost" icon="globe" iconOnly aria-label="Open the web version" title="Open the web version" onClick={() => openWeb('/debug', { title: 'Debug (web)' })} />
      </div>
      <div class="view-body dbg-split">
        <TopicBody e={e} webPath="/debug" openWeb={(p) => openWeb(p)}>
          {() => !rows.length ? <EmptyState icon="debug" text={q || kind !== 'all' ? 'No report matches.' : 'No debug runs or reports yet.'} /> : (
            <>
              <VirtualList class="dbg-list" items={rows} rowHeight={ROW_H} focusIndex={idx} tabIndex={0} role="listbox" aria-label="Debug reports"
                data-testid="debug-list" onKeyDown={onKey}
                row={(r) => (
                  <div key={debugId(r)} class="list-row dbg-row" role="option" aria-selected={debugId(r) === cur} data-cur={debugId(r) === cur ? '1' : undefined}
                    style={{ height: `${ROW_H}px` }} data-testid={`debug-row-${r.name}`} onClick={() => setSel(debugId(r))}>
                    <div class="row dbg-l1">
                      <span class="ellipsis grow">{r.title || r.name}</span>
                      {r.verdict ? <Pill tone={verdictTone(r.verdict)} dot={r.verdict === 'running' ? 'live' : undefined}>{r.verdict}</Pill> : null}
                    </div>
                    <div class="row meta dbg-l2">
                      <span>{r.kind === 'runs' ? 'run' : 'report'}</span>
                      {r.key ? <span class="mono">{r.key}</span> : null}
                      <span class="ellipsis grow mono faint">{r.name}</span>
                      <span class="tnum">{ago(r.mtime)}</span>
                    </div>
                  </div>
                )} />
              {row ? <Report r={row} /> : null}
            </>
          )}
        </TopicBody>
      </div>
    </div>
  );
}

function Report({ r }: { r: DebugRow }) {
  const [text, setText] = useState<string | null>(null);
  const [err, setErr] = useState<{ code: string; message: string } | null>(null);
  const [n, setN] = useState(0);
  useEffect(() => {
    let live = true;
    setText(null); setErr(null);
    void bridge.api.get<{ text: string }>(api(reportPath(r))).then((res) => {
      if (!live) return;
      if (res.ok) setText(res.data?.text ?? ''); else setErr(res.error);
    });
    return () => { live = false; };
  }, [r.kind, r.name, r.mtime, n]);
  return (
    <section class="dbg-report" aria-label="Report" data-testid="debug-report">
      <div class="toolbar">
        <span class="ellipsis grow dbg-title">{r.title || r.name}</span>
        {r.change ? <Pill tone="accent">code change</Pill> : null}
        {r.key ? <Button size="sm" variant="ghost" icon="queue" onClick={() => openPanel('ticket', r.key, { title: r.key, params: { key: r.key } })}>{r.key}</Button> : null}
        {r.session ? <Button size="sm" variant="ghost" icon="chat" testid="debug-session"
          onClick={() => openPanel('transcript', r.session, { title: r.title || r.session.slice(0, 12), params: { sid: r.session } })}>Transcript</Button> : null}
        <Button size="sm" variant="ghost" icon="refresh" iconOnly aria-label="Reload report" title="Reload" onClick={() => setN(n + 1)} />
        <Button size="sm" variant="ghost" icon="globe" iconOnly aria-label="Open the web version" title="Open the web version"
          onClick={() => openWeb(`/debug/${encodeURIComponent(r.kind)}/${encodeURIComponent(r.name)}`, { title: `${r.name} (web)` })} />
      </div>
      <div class="dbg-text">
        {err ? (err.code === 'not-found' ? <EmptyState icon="debug" text={r.verdict === 'running' ? 'Still looking; no report yet.' : 'This report is gone.'} /> : <ErrorPanel error={err} onRetry={() => setN(n + 1)} />)
          : text === null ? <Skeleton rows={8} />
            : text.trim() ? <Markdown text={text} testid="debug-report-md" />
              : <EmptyState icon="debug" text={r.verdict === 'running' ? 'Still looking; no report yet.' : 'This report is empty.'} />}
      </div>
    </section>
  );
}
