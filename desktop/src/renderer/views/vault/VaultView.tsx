// Vault view: the active vault's pages (filtered, windowed), its folder tree (lazy /vault/tree), ingest status
// (/vault/ingest: run, per-source summary, failures, output tail) and recent commits; a vault switcher when several
// exist (/vaults + POST /vault/use). Pages and files open in the vault-file panel.
import { useEffect, useMemo, useState } from 'preact/hooks';
import type { Vaults } from '../../../shared/api';
import { act, store, useTopic } from '../../core/app';
import { api, bridge } from '../../core/bridge';
import { ago, bytes, plural } from '../../core/format';
import { openPanel, openWeb, type PanelCtx } from '../../shell/dock';
import { AsOf, Button, EmptyState, ErrorPanel, Pill, Skeleton, Tabs, TopicBody } from '../../ui/kit';
import { Icon } from '../../ui/Icon';
import { VirtualList } from '../queue/virtual';
import { filterPages, flattenTree, ingestFailures, ingestSources, ingestStatus, type PageRow, type TreeEntry } from './model';

type Sec = 'pages' | 'files' | 'ingest' | 'log';
const SECS: Sec[] = ['pages', 'files', 'ingest', 'log'];
export const openVaultFile = (rel: string) =>
  openPanel('vault-file', rel, { title: rel.slice(rel.lastIndexOf('/') + 1) || rel, params: { path: rel } });

export default function VaultView({ ctx }: { ctx: PanelCtx }) {
  const e = useTopic('vault');
  const p0 = ctx.params.value.sec;
  const [sec, setSec] = useState<Sec>(SECS.includes(p0 as Sec) ? (p0 as Sec) : 'pages');
  useEffect(() => { ctx.setTitle('Vault'); }, []);
  const pick = (s: Sec) => { setSec(s); ctx.update({ sec: s }); };
  const d = e?.data.value;
  const noVault = !d && e?.error.value?.code === 'unavailable';
  const [reloadN, setReloadN] = useState(0);
  return (
    <div class="view vault-view" data-testid="view-vault">
      <div class="view-head">
        <Icon name="vault" size={16} />
        <span class="vt-name ellipsis" data-testid="vault-name">{d ? d.name : 'Vault'}</span>
        <VaultSwitcher active={d?.name ?? ''} />
        <span class="grow" />
        <AsOf e={e} />
        <Button size="sm" variant="ghost" icon="refresh" iconOnly aria-label="Reload" title="Reload" onClick={() => { void store.load('vault'); void store.load('ingest'); setReloadN((x) => x + 1); }} testid="vault-reload" />
        <Button size="sm" variant="ghost" icon="globe" iconOnly aria-label="Open the web version" title="Open the web version" onClick={() => openWeb('/vault', { title: 'Vault (web)' })} />
      </div>
      {noVault ? (
        <div class="view-body"><EmptyState icon="vault" text="No vault is active here. Pick one above, or add one with harness vault." /></div>
      ) : (
        <>
          <Tabs testidPrefix="vault-tab-" value={sec} onChange={pick} tabs={[
            { id: 'pages', label: 'Pages', badge: d ? String(d.pages.length) : undefined },
            { id: 'files', label: 'Files' },
            { id: 'ingest', label: 'Ingest' },
            { id: 'log', label: 'History' },
          ]} />
          <div class="view-body vt-body">
            <TopicBody e={e} webPath="/vault" openWeb={(p) => openWeb(p)}>
              {(v) => (
                sec === 'pages' ? <Pages pages={(v.pages ?? []) as PageRow[]} />
                  : sec === 'files' ? <Files key={`${v.name}:${reloadN}`} />
                    : sec === 'ingest' ? <IngestPane />
                      : <Log log={(v as { log?: { sha: string; author: string; when: string; subject: string }[] }).log ?? []} />
              )}
            </TopicBody>
          </div>
        </>
      )}
    </div>
  );
}

function VaultSwitcher({ active }: { active: string }) {
  const [vs, setVs] = useState<Vaults | null>(null);
  useEffect(() => { void bridge.api.get<Vaults>(api('/vaults')).then((r) => { if (r.ok && r.data) setVs(r.data); }); }, [active]);
  const list = vs?.vaults ?? [];
  if (list.length < 2) return null;
  return (
    <select class="select vt-switch" aria-label="Active vault" data-testid="vault-switch" value={vs?.active ?? active}
      onChange={(ev) => { const name = (ev.target as HTMLSelectElement).value; void act('/vault/use', { name }, { refresh: ['vault', 'ingest'] }); }}>
      {list.map((v) => <option key={v.name} value={v.name}>{v.name}</option>)}
    </select>
  );
}

const ROW_H = 30;
function Pages({ pages }: { pages: PageRow[] }) {
  const [q, setQ] = useState('');
  const [cur, setCur] = useState(0);
  const list = useMemo(() => filterPages(pages, q), [pages, q]);
  const onKey = (ev: KeyboardEvent) => {
    if (ev.key === 'ArrowDown') { ev.preventDefault(); setCur((c) => Math.min(list.length - 1, c + 1)); }
    else if (ev.key === 'ArrowUp') { ev.preventDefault(); setCur((c) => Math.max(0, c - 1)); }
    else if (ev.key === 'Enter' && list[cur]) { ev.preventDefault(); openVaultFile(list[cur].rel); }
  };
  return (
    <div class="vt-col">
      <div class="toolbar">
        <label class="search-field"><Icon name="search" size={13} />
          <input class="input input--bare" placeholder="Filter pages" aria-label="Filter pages" data-testid="vault-filter" value={q}
            onInput={(ev) => { setQ((ev.target as HTMLInputElement).value); setCur(0); }} onKeyDown={onKey} />
        </label>
        <span class="meta tnum">{q ? `${list.length} of ${pages.length}` : plural(pages.length, 'page')}</span>
      </div>
      {!list.length ? <EmptyState icon="file" text={q ? 'No page matches the filter.' : 'This vault has no pages yet.'} /> : (
        <VirtualList class="vt-list" items={list} rowHeight={ROW_H} focusIndex={cur} tabIndex={0} role="listbox" aria-label="Pages"
          data-testid="vault-pages" onKeyDown={onKey}
          row={(p, i) => (
            <div key={p.rel} class="list-row vt-row" role="option" aria-selected={i === cur} data-cur={i === cur ? '1' : undefined}
              style={{ height: `${ROW_H}px` }} data-testid="vault-page" onClick={() => { setCur(i); openVaultFile(p.rel); }}>
              <Icon name="file" size={13} />
              <span class="ellipsis grow">{p.title || p.rel}</span>
              <span class="meta mono ellipsis vt-rel">{p.rel}</span>
              {p.mtime ? <span class="meta tnum">{ago(p.mtime)}</span> : null}
            </div>
          )} />
      )}
    </div>
  );
}

function Files() {
  const [kids, setKids] = useState<Record<string, TreeEntry[] | undefined>>({});
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState<Set<string>>(new Set());
  const [err, setErr] = useState<{ code: string; message: string } | null>(null);
  const [cur, setCur] = useState(0);
  const fetchDir = async (path: string) => {
    setLoading((s) => new Set(s).add(path));
    const r = await bridge.api.get<{ entries: TreeEntry[] }>(api('/vault/tree'), path ? { path } : {});
    setLoading((s) => { const n = new Set(s); n.delete(path); return n; });
    if (!r.ok) { setErr(r.error); return; }
    setErr(null);
    setKids((k) => ({ ...k, [path]: r.data?.entries ?? [] }));
  };
  useEffect(() => { void fetchDir(''); }, []);
  const rows = useMemo(() => flattenTree(kids, open, loading), [kids, open, loading]);
  const toggle = (e: TreeEntry) => {
    if (!e.dir) { openVaultFile(e.path); return; }
    const n = new Set(open);
    if (n.has(e.path)) n.delete(e.path); else { n.add(e.path); if (!kids[e.path]) void fetchDir(e.path); }
    setOpen(n);
  };
  const onKey = (ev: KeyboardEvent) => {
    const r = rows[cur];
    if (ev.key === 'ArrowDown') { ev.preventDefault(); setCur((c) => Math.min(rows.length - 1, c + 1)); }
    else if (ev.key === 'ArrowUp') { ev.preventDefault(); setCur((c) => Math.max(0, c - 1)); }
    else if (ev.key === 'Enter' && r) { ev.preventDefault(); toggle(r.e); }
    else if (ev.key === 'ArrowRight' && r?.e.dir && !r.open) { ev.preventDefault(); toggle(r.e); }
    else if (ev.key === 'ArrowLeft' && r?.e.dir && r.open) { ev.preventDefault(); toggle(r.e); }
  };
  if (!kids[''] && err) return <ErrorPanel error={err} onRetry={() => fetchDir('')} />;
  if (!kids['']) return <Skeleton rows={6} />;
  if (!rows.length) return <EmptyState icon="folder" text="The vault folder is empty." />;
  return (
    <div class="vt-col">
      {err ? <div class="inline-err" role="alert">{err.message || err.code}</div> : null}
      <VirtualList class="vt-list" items={rows} rowHeight={ROW_H} focusIndex={cur} tabIndex={0} role="tree" aria-label="Vault files"
        data-testid="vault-tree" onKeyDown={onKey}
        row={(r, i) => (
          <div key={r.e.path} class="list-row vt-row" role="treeitem" aria-expanded={r.e.dir ? r.open : undefined}
            aria-selected={i === cur} data-cur={i === cur ? '1' : undefined} data-testid="vault-tree-row"
            style={{ height: `${ROW_H}px`, paddingLeft: `${8 + r.depth * 16}px` }} onClick={() => { setCur(i); toggle(r.e); }}>
            {r.e.dir ? <Icon name={r.open ? 'chevronDown' : 'chevronRight'} size={12} /> : <span class="vt-sp" />}
            <Icon name={r.e.dir ? 'folder' : 'file'} size={13} />
            <span class="ellipsis grow">{r.e.name}</span>
            {r.loading ? <span class="spin" aria-label="Loading" /> : null}
            {!r.e.dir ? <span class="meta tnum">{bytes(r.e.size)}</span> : null}
          </div>
        )} />
    </div>
  );
}

function IngestPane() {
  const e = useTopic('ingest');
  return (
    <TopicBody e={e} webPath="/vault" openWeb={(p) => openWeb(p)}>
      {(ing) => {
        const st = ingestStatus(ing as never);
        const srcs = ingestSources(ing.summary);
        const fails = ingestFailures(ing.summary);
        const sum = (ing.summary ?? {}) as Record<string, unknown>;
        const tail = typeof ing.out_tail === 'string' ? ing.out_tail : '';
        return (
          <div class="vt-ingest" data-testid="vault-ingest">
            <div class="row">
              <Pill tone={st.tone} dot={st.running ? 'live' : undefined} testid="vault-ingest-status">{st.label}</Pill>
              {typeof sum.last === 'string' && sum.last ? <span class="meta ellipsis">{sum.last}</span> : null}
              <span class="spacer" />
              {st.running ? <Button size="sm" icon="stop" testid="vault-ingest-stop" onClick={() => act('/vault/ingest/stop', {}, { refresh: ['ingest'] })}>Stop</Button> : (
                <>
                  {fails.length ? <Button size="sm" icon="retry" onClick={() => act('/vault/ingest/start', { what: 'retry' }, { refresh: ['ingest'] })}>Retry failed</Button> : null}
                  <Button size="sm" icon="play" testid="vault-ingest-start" onClick={() => act('/vault/ingest/start', {}, { refresh: ['ingest'] })}>Start ingest</Button>
                </>
              )}
            </div>
            {srcs.length ? (
              <table class="vt-table">
                <thead><tr><th>Source</th><th class="tnum">Pages</th><th class="tnum">OK</th><th class="tnum">Failed</th><th>Last</th></tr></thead>
                <tbody>{srcs.map((s) => (
                  <tr key={s.name}>
                    <td><span class="mono">{s.name}</span>{!s.exists ? <> <Pill tone="warn">missing</Pill></> : null}</td>
                    <td class="tnum">{s.pages}</td><td class="tnum">{s.ok}</td><td class={`tnum ${s.failed ? 'dv-del' : ''}`}>{s.failed}</td>
                    <td class="meta">{s.at ? ago(s.at) : ''}</td>
                  </tr>
                ))}</tbody>
              </table>
            ) : <div class="meta">No source has been ingested yet.</div>}
            {fails.length ? (
              <>
                <div class="section-title">Failed units</div>
                <ul class="vt-fails">{fails.slice(0, 50).map((f) => (
                  <li key={f.id}><span class="mono">{f.id}</span> <span class="meta">{plural(f.attempts, 'attempt')}</span>
                    {f.problems.map((p, i) => <div key={i} class="meta tk-pre">{p}</div>)}</li>
                ))}</ul>
              </>
            ) : null}
            {tail ? <><div class="section-title">Output</div><pre class="pre vt-tail" data-testid="vault-ingest-tail">{tail}</pre></> : null}
          </div>
        );
      }}
    </TopicBody>
  );
}

function Log({ log }: { log: { sha: string; author: string; when: string; subject: string }[] }) {
  if (!log.length) return <EmptyState icon="archive" text="No commits yet." />;
  return (
    <ul class="vt-log" data-testid="vault-log">
      {log.map((c) => (
        <li key={c.sha} class="list-row">
          <span class="mono faint">{String(c.sha).slice(0, 8)}</span>
          <span class="ellipsis grow">{c.subject}</span>
          <span class="meta">{c.author}</span>
          <span class="meta tnum">{c.when}</span>
        </li>
      ))}
    </ul>
  );
}
