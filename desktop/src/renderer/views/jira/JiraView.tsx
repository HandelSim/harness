// Jira view: serve's GET /jira (sprint or backlog, optionally only mine), filtered and windowed, with each row's queue
// state. Picked keys can be copied or carried into the Add tickets dialog (the API adds tickets with a project, which
// that dialog asks for).
import { useEffect, useMemo, useRef, useState } from 'preact/hooks';
import type { Jira, JiraRow } from '../../../shared/api';
import { api, bridge } from '../../core/bridge';
import { plural } from '../../core/format';
import { toast } from '../../core/toast';
import { hooks } from '../../shell/commands';
import { openPanel, openWeb, type PanelCtx } from '../../shell/dock';
import { Button, EmptyState, ErrorPanel, Pill, Seg, Skeleton } from '../../ui/kit';
import { Icon } from '../../ui/Icon';
import { VirtualList } from '../queue/virtual';
import { addableKeys, filterRows, jiraQuery, JIRA_LISTS, statusTone, type JiraList } from './model';

const ROW_H = 34;

export default function JiraView({ ctx }: { ctx: PanelCtx }) {
  const p = ctx.params.value;
  const [list, setList] = useState<JiraList>(JIRA_LISTS.includes(p.list as JiraList) ? (p.list as JiraList) : 'sprint');
  const [mine, setMine] = useState(p.mine === true);
  const [d, setD] = useState<Jira | null>(null);
  const [err, setErr] = useState<{ code: string; message: string } | null>(null);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState('');
  const [cur, setCur] = useState(0);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const seq = useRef(0);
  useEffect(() => { ctx.setTitle('Jira'); }, []);
  const load = async (l = list, m = mine) => {
    const n = ++seq.current;
    setLoading(true);
    const r = await bridge.api.get<Jira>(api('/jira'), jiraQuery(l, m));
    if (n !== seq.current) return;
    setLoading(false);
    if (!r.ok) { setErr(r.error); return; }
    setErr(null);
    setD(r.data ?? null);
  };
  useEffect(() => { void load(); }, []);
  const choose = (l: JiraList, m: boolean) => { setList(l); setMine(m); setPicked(new Set()); setCur(0); ctx.update({ list: l, mine: m }); void load(l, m); };
  const rows = useMemo(() => filterRows(d?.rows ?? [], q), [d, q]);
  const keys = addableKeys(rows, picked);
  const toggle = (k: string) => { const n = new Set(picked); if (n.has(k)) n.delete(k); else n.add(k); setPicked(n); };
  const copy = async (ks: string[]) => {
    try { await navigator.clipboard.writeText(ks.join(' ')); toast({ kind: 'ok', title: `Copied ${plural(ks.length, 'key')}` }); }
    catch { toast({ kind: 'warn', title: 'Could not copy to the clipboard' }); }
  };
  const addAll = async () => {
    if (keys.length) await copy(keys);
    hooks.addTickets();
    if (keys.length) toast({ kind: 'info', title: 'Paste the keys into Ticket keys' });
  };
  const openRow = (r: JiraRow) => {
    if (r.queued) openPanel('ticket', r.key, { title: r.key, params: { key: r.key } });
    else toggle(r.key);
  };
  const onKey = (ev: KeyboardEvent) => {
    if (ev.key === 'ArrowDown') { ev.preventDefault(); setCur((c) => Math.min(rows.length - 1, c + 1)); }
    else if (ev.key === 'ArrowUp') { ev.preventDefault(); setCur((c) => Math.max(0, c - 1)); }
    else if (ev.key === ' ' && rows[cur]) { ev.preventDefault(); toggle(rows[cur].key); }
    else if (ev.key === 'Enter' && rows[cur]) { ev.preventDefault(); openRow(rows[cur]); }
  };
  return (
    <div class="view jira-view" data-testid="view-jira">
      <div class="view-head">
        <Seg label="List" value={list} onChange={(l) => choose(l, mine)} options={[{ value: 'sprint', label: 'Sprint' }, { value: 'backlog', label: 'Backlog' }]} />
        <label class="row jr-mine"><input type="checkbox" checked={mine} data-testid="jira-mine" onChange={(e) => choose(list, (e.target as HTMLInputElement).checked)} /> Only mine</label>
        <label class="search-field"><Icon name="search" size={13} />
          <input class="input input--bare" placeholder="Filter" aria-label="Filter Jira tickets" data-testid="jira-filter" value={q}
            onInput={(e) => { setQ((e.target as HTMLInputElement).value); setCur(0); }} onKeyDown={onKey} />
        </label>
        <span class="grow" />
        {loading && d ? <span class="spin" aria-label="Loading" /> : null}
        <Button size="sm" variant="ghost" icon="copy" disabled={!keys.length} onClick={() => copy(keys)} testid="jira-copy">Copy {keys.length ? keys.length : ''}</Button>
        <Button size="sm" icon="plus" onClick={addAll} testid="jira-add">Add tickets…</Button>
        <Button size="sm" variant="ghost" icon="refresh" iconOnly aria-label="Reload" title="Reload" onClick={() => load()} testid="jira-reload" />
        <Button size="sm" variant="ghost" icon="globe" iconOnly aria-label="Open the web version" title="Open the web version" onClick={() => openWeb(`/jira?list=${list}${mine ? '&mine=1' : ''}`, { title: 'Jira (web)' })} />
      </div>
      <div class="view-body jr-body">
        {!d ? (err ? <ErrorPanel error={err} onRetry={() => load()} /> : <Skeleton rows={8} />) : (
          <>
            {err ? <div class="inline-err" role="alert">{err.message || err.code}</div> : null}
            {d.error ? <div class="notice notice--warn" data-testid="jira-error"><Icon name="alert" size={16} /><div>{d.error}</div></div> : null}
            {d.note ? <div class="notice" data-testid="jira-note"><Icon name="info" size={16} /><div>{d.note}</div></div> : null}
            {!rows.length ? (d.error ? null : <EmptyState icon="jira" text={q ? 'No ticket matches the filter.' : mine ? 'Nothing assigned to you in this list.' : 'This list is empty.'} />) : (
              <VirtualList class="jr-list" items={rows} rowHeight={ROW_H} focusIndex={cur} tabIndex={0} role="listbox" aria-multiselectable="true"
                aria-label="Jira tickets" data-testid="jira-rows" onKeyDown={onKey}
                row={(r, i) => (
                  <div key={r.key} class="list-row jr-row" role="option" aria-selected={picked.has(r.key)} data-cur={i === cur ? '1' : undefined}
                    style={{ height: `${ROW_H}px` }} data-testid={`jira-row-${r.key}`} onClick={() => { setCur(i); openRow(r); }}>
                    <input type="checkbox" tabIndex={-1} checked={picked.has(r.key)} disabled={!!r.queued} aria-label={`Pick ${r.key}`}
                      onClick={(e) => { e.stopPropagation(); setCur(i); toggle(r.key); }} />
                    <span class="mono jr-key">{r.key}</span>
                    <span class="ellipsis grow" title={r.summary}>{r.summary}</span>
                    <span class="meta ellipsis jr-type">{r.type}</span>
                    <span class="meta ellipsis jr-who">{r.assignee || 'Unassigned'}</span>
                    <Pill tone={statusTone(r)}>{r.status}</Pill>
                    {r.queued ? <Pill tone="accent" testid={`jira-queued-${r.key}`}>{`queued: ${r.queued}`}</Pill> : null}
                  </div>
                )} />
            )}
            {d.more ? <div class="meta jr-more">Showing the first {d.rows.length}; open the web version or Jira for the rest.</div> : null}
          </>
        )}
      </div>
    </div>
  );
}
