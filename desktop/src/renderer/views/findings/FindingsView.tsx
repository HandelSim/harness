// Findings view: the board (GET /findings; archive/restore/move), the findings agent card (GET /findings/agent;
// start/stop/notes/folders) and what it has learned (GET /findings/notes/<slug>/<name>, rendered with ui/Markdown).
// Parity with serve's /runs/findings, /runs/findings/agent and /runs/findings/notes.
import { useEffect, useMemo, useState } from 'preact/hooks';
import type { ComponentChildren } from 'preact';
import type { FindingsAgent } from '../../../shared/api';
import type { TopicEntry } from '../../core/store';
import { act, store, useTopic } from '../../core/app';
import { api, bridge } from '../../core/bridge';
import { ago, dateTime, basename } from '../../core/format';
import { isTyping } from '../../core/keymap';
import { toast } from '../../core/toast';
import { openPanel, openWeb, type PanelCtx } from '../../shell/dock';
import { VIEW_WEB, WEB } from '../../shell/panels';
import { closeDialog, openDialog } from '../../shell/ui';
import { AsOf, Button, EmptyState, ErrorPanel, Pill, Skeleton, Tabs } from '../../ui/kit';
import { Icon } from '../../ui/Icon';
import { Markdown } from '../../ui/Markdown';
import { VirtualList } from '../queue/virtual';
import {
  agentView, board, copyText, extraFolders, facets, filterFindings, folderChoices, groupKnowledge, moveTarget, NOTES_MAX, notePath,
  type FindingView, type NoteRef,
} from './model';
import css from './findings.css' with { type: 'text' };

if (!document.getElementById('css-findings')) { const el = document.createElement('style'); el.id = 'css-findings'; el.textContent = css as string; document.head.append(el); }

type Tab = 'open' | 'archived' | 'agent' | 'notes';
const ROW_H = 34;
const REFRESH = ['findings', 'findings-agent'] as const;
const SEV_TONE: Record<string, 'err' | 'warn' | 'default'> = { high: 'err', medium: 'warn', low: 'default' };
const STOP_TEXT = 'Its session in flight is ended and that pass runs again later. It stays stopped, also after a restart, until you start it.';

function confirmDialog(title: string, body: string, confirm: string): Promise<boolean> {
  return new Promise((resolve) => {
    const done = (v: boolean) => { closeDialog(); resolve(v); };
    openDialog(
      <div class="dialog glass glass--overlay" role="alertdialog" aria-label={title} data-testid="findings-confirm">
        <header>{title}</header>
        <div class="dialog-body"><p class="muted">{body}</p></div>
        <footer><Button variant="ghost" onClick={() => done(false)}>Cancel</Button><Button variant="danger" testid="findings-confirm-ok" onClick={() => done(true)} autoFocus>{confirm}</Button></footer>
      </div>,
    );
  });
}

const copy = (text: string, what: string) => {
  void navigator.clipboard?.writeText(text).then(() => toast({ kind: 'ok', title: `${what} copied` }), () => toast({ kind: 'warn', title: 'Copy failed' }));
};
const watch = async () => {
  const r = await bridge.api.get<{ sid: string }>(api('/follow'), { findings: '1' });
  if (r.ok && r.data?.sid) openPanel('transcript', r.data.sid, { title: 'Findings agent', params: { sid: r.data.sid } });
  else toast({ kind: 'warn', title: 'No findings agent session to watch', body: r.ok ? '' : r.error.message });
};
const openSession = (sid: string, title: string) => openPanel('transcript', sid, { title, params: { sid } });

// ---- board -------------------------------------------------------------------------------------------------

function Detail({ f }: { f: FindingView }) {
  const archived = f.archived;
  const items = f.rolling.slice(0, 50);
  return (
    <div class="fdetail" data-testid={`finding-detail-${f.id}`}>
      <div class="row fdetail-tags">
        {f.cat ? <Pill>{f.cat}</Pill> : null}
        {f.sev ? <Pill tone={SEV_TONE[f.sev] ?? 'default'}>{f.sev}</Pill> : null}
        {archived ? <Pill>archived</Pill> : null}
        <span class="grow" />
        <Button size="sm" icon="copy" onClick={() => copy(copyText(f), 'Finding')}>Copy</Button>
        {archived ? <Button size="sm" icon="retry" testid="finding-restore" onClick={() => void act('/findings/restore', { id: f.id }, { refresh: [...REFRESH] })}>Restore</Button>
          : <Button size="sm" icon="archive" testid="finding-archive" onClick={() => void act('/findings/archive', { id: f.id }, { refresh: [...REFRESH] })}>Archive</Button>}
      </div>
      <h2 class="fdetail-title">{f.title}</h2>
      <dl class="kv">
        {f.path ? <><dt>where</dt><dd class="mono">{f.path}{f.line ? `:${f.line}` : ''}</dd></> : null}
        <dt>from</dt>
        <dd>{f.key ? <a href="#" data-testid="finding-ticket" onClick={(e) => { e.preventDefault(); openPanel('ticket', f.key, { title: f.key, params: { key: f.key } }); }}>{f.key}</a>
          : <span>findings agent{f.project ? ` · ${basename(f.project)}` : ''}</span>}</dd>
        {f.ms ? <><dt>noted</dt><dd title={dateTime(f.ms)}>{ago(f.ms)}</dd></> : null}
      </dl>
      {f.body ? <Markdown text={f.body} class="fdetail-body" /> : null}
      {f.evidence ? <><div class="section-title">Evidence</div><pre class="pre fdetail-pre">{f.evidence}</pre></> : null}
      {f.bug ? (
        <div class="col">
          {f.bug.ui ? <><div class="section-title">Where a user meets it</div><p>{f.bug.ui}</p></> : null}
          {f.bug.steps.length ? <><div class="section-title">Steps</div><ol class="fdetail-steps">{f.bug.steps.map((s, i) => <li key={i}>{s}</li>)}</ol></> : null}
          {f.bug.expected ? <><div class="section-title">Expected</div><p>{f.bug.expected}</p></> : null}
          {f.bug.actual ? <><div class="section-title">Actual</div><p>{f.bug.actual}</p></> : null}
          {f.bug.trace.length ? <><div class="section-title">Trace</div><pre class="pre fdetail-pre">{f.bug.trace.join('\n')}</pre></> : null}
          {f.bug.verdict ? <><div class="section-title">Checker's verdict</div><p>{f.bug.verdict}</p></> : null}
        </div>
      ) : null}
      {items.length ? (
        <div class="col">
          <div class="section-title">{f.rolling.length} item{f.rolling.length === 1 ? '' : 's'}</div>
          <ul class="fitems">{items.map((i) => (
            <li key={i.id}>
              <span class="mono">{i.path}{i.line ? `:${i.line}` : ''}</span>{i.note ? <span> {i.note}</span> : null}
              {i.evidence ? <code> {i.evidence}</code> : null}
              {!archived ? <Button size="sm" variant="ghost" title="Take it off the list" onClick={() => void act('/findings/archive', { id: i.id }, { refresh: [...REFRESH] })}>Not this one</Button> : null}
            </li>
          ))}</ul>
          {f.rolling.length > items.length ? <span class="meta">{f.rolling.length - items.length} more</span> : null}
        </div>
      ) : null}
    </div>
  );
}

function Board({ rows, total, archived, q, src, cat }: { rows: FindingView[]; total: number; archived: boolean; q: string; src: string; cat: string }) {
  const list = useMemo(() => filterFindings(rows, { q, src, cat }), [rows, q, src, cat]);
  const [sel, setSel] = useState<string | null>(null);
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const cur = list.find((r) => r.id === sel) ?? list[0] ?? null;
  const idx = cur ? list.indexOf(cur) : -1;
  // drop checks of rows that left the board
  useEffect(() => { setChecked((c) => { const ids = new Set(rows.map((r) => r.id)); const n = new Set([...c].filter((x) => ids.has(x))); return n.size === c.size ? c : n; }); }, [rows]);
  const toggle = (id: string) => setChecked((c) => { const n = new Set(c); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  const allOn = list.length > 0 && list.every((r) => checked.has(r.id));
  const picked = list.filter((r) => checked.has(r.id));
  const move = (id: string, dir: -1 | 1) => {
    if (archived || q || src || cat) return;   // the board's order is only meaningful unfiltered
    const before = moveTarget(rows.map((r) => r.id), id, dir);
    if (before !== null) void act('/findings/move', { key: id, before }, { refresh: ['findings'], quiet: true });
  };
  const onKey = (e: KeyboardEvent) => {
    if (isTyping(e.target) || !list.length) return;
    const go = (i: number) => { e.preventDefault(); setSel(list[Math.max(0, Math.min(list.length - 1, i))].id); };
    if (e.altKey && (e.key === 'ArrowUp' || e.key === 'ArrowDown') && cur) { e.preventDefault(); move(cur.id, e.key === 'ArrowUp' ? -1 : 1); return; }
    if (e.key === 'ArrowDown') go(idx + 1);
    else if (e.key === 'ArrowUp') go(idx - 1);
    else if (e.key === 'Home') go(0);
    else if (e.key === 'End') go(list.length - 1);
    else if (e.key === ' ' && cur) { e.preventDefault(); toggle(cur.id); }
  };
  if (!rows.length) {
    return <EmptyState icon="findings" text={archived ? 'Nothing archived.' : "No findings yet. A stage agent that notices a problem outside its ticket notes it here (and in the ticket's findings note); the findings agent adds what it finds."} />;
  }
  return (
    <div class="fsplit">
      <div class="fleft">
        <div class="toolbar fbar">
          <label class="check"><input type="checkbox" aria-label="Select all shown" checked={allOn}
            onChange={() => setChecked(allOn ? new Set() : new Set(list.map((r) => r.id)))} />{picked.length ? `${picked.length} selected` : `${list.length}${list.length !== total ? ` of ${total}` : ''}`}</label>
          <span class="spacer" />
          <Button size="sm" icon="copy" disabled={!picked.length} onClick={() => copy(picked.map(copyText).join('\n'), `${picked.length} findings`)}>Copy</Button>
          {archived ? <Button size="sm" icon="retry" disabled={!picked.length} testid="findings-restore-selected"
            onClick={async () => { const r = await act('/findings/restore', { sel: picked.map((p) => p.id) }, { refresh: [...REFRESH] }); if (r.ok) setChecked(new Set()); }}>Restore</Button>
            : <Button size="sm" icon="archive" disabled={!picked.length} testid="findings-archive-selected"
              onClick={async () => { const r = await act('/findings/archive', { sel: picked.map((p) => p.id) }, { refresh: [...REFRESH] }); if (r.ok) setChecked(new Set()); }}>Archive</Button>}
        </div>
        {!list.length ? <EmptyState icon="search" text="No finding matches the filter." /> : (
          <VirtualList class="flist" role="listbox" aria-label={archived ? 'Archived findings' : 'Open findings'} tabIndex={0} data-testid={archived ? 'findings-archived-list' : 'findings-list'}
            aria-activedescendant={cur ? `frow-${cur.id}` : undefined} onKeyDown={onKey} items={list} rowHeight={ROW_H} focusIndex={idx}
            row={(r: FindingView) => (
              <div key={r.id} id={`frow-${r.id}`} class="frow" role="option" aria-selected={r === cur} data-testid={`finding-${r.id}`} onClick={() => setSel(r.id)}>
                <input type="checkbox" tabIndex={-1} aria-label={`Select ${r.title}`} checked={checked.has(r.id)} onClick={(e) => e.stopPropagation()} onChange={() => toggle(r.id)} />
                <span class={`fsev fsev--${r.sev || 'none'}`} title={r.sev || 'no severity'} />
                <span class="ellipsis frow-title">{r.title}</span>
                <span class="meta ellipsis frow-src">{r.key || 'agent'}</span>
                <span class="meta tnum frow-ago">{r.ms ? ago(r.ms) : ''}</span>
              </div>
            )} />
        )}
        {!archived && !q && !src && !cat && list.length > 1 ? <div class="meta fhint">Alt+↑/↓ moves the selected finding; the board's order is shared with the web page.</div> : null}
      </div>
      <div class="fright">{cur ? <Detail key={cur.id} f={cur} /> : <EmptyState icon="findings" text="Select a finding." />}</div>
    </div>
  );
}

// ---- agent -------------------------------------------------------------------------------------------------

function StartForm({ fa, onDone }: { fa: FindingsAgent; onDone: () => void }) {
  const choices = useMemo(() => folderChoices(fa), [fa]);
  const [on, setOn] = useState<Set<string>>(() => new Set(choices.filter((c) => c.on).map((c) => c.dir)));
  const [more, setMore] = useState('');
  const [busy, setBusy] = useState(false);
  const dirs = choices.filter((c) => on.has(c.dir)).map((c) => c.dir);
  const extra = extraFolders(more, dirs);
  const run = async (path: string, body: Record<string, unknown>) => { setBusy(true); const r = await act(path, body, { refresh: [...REFRESH] }); setBusy(false); if (r.ok) onDone(); };
  return (
    <div class="card col" data-testid="findings-start-form">
      <div class="section-title">Folders to explore</div>
      {choices.length ? choices.map((c) => (
        <label key={c.dir} class="check"><input type="checkbox" checked={on.has(c.dir)}
          onChange={() => setOn((s) => { const n = new Set(s); if (n.has(c.dir)) n.delete(c.dir); else n.add(c.dir); return n; })} /><span class="mono ellipsis">{c.dir}</span></label>
      )) : <span class="meta">No folder chosen before: serve's own project is explored unless you add folders below.</span>}
      <textarea class="textarea" rows={3} placeholder="Other folders, one per line" aria-label="Other folders, one per line" value={more} onInput={(e) => setMore((e.target as HTMLTextAreaElement).value)} />
      <div class="toolbar">
        <Button variant="primary" icon="play" disabled={busy} testid="findings-start-go" onClick={() => void run('/findings/agent/start', { pick: 1, dir: dirs, more: extra })}>Start</Button>
        <Button disabled={busy} onClick={() => void run('/findings/agent/folders', { dir: [...dirs, ...extra] })}>Save for the next start</Button>
        {Array.isArray(fa.folders) && fa.folders.length ? <Button variant="ghost" disabled={busy} onClick={() => void run('/findings/agent/folders', { act: 'reset' })}>Forget the choice</Button> : null}
        <span class="spacer" />
        <Button variant="ghost" onClick={onDone}>Cancel</Button>
      </div>
    </div>
  );
}

function Notes({ fa }: { fa: FindingsAgent }) {
  const [text, setText] = useState(fa.notes || '');
  const [busy, setBusy] = useState(false);
  useEffect(() => { setText(fa.notes || ''); }, [fa.notes]);
  const dirty = text !== (fa.notes || '');
  return (
    <div class="card col">
      <div class="section-title">Notes for every pass</div>
      <span class="meta">Given to each of its sessions: what to look at, what to leave alone.</span>
      <textarea class="textarea" rows={4} maxLength={NOTES_MAX} aria-label="Notes for every pass" data-testid="findings-notes" value={text} onInput={(e) => setText((e.target as HTMLTextAreaElement).value)} />
      <div class="toolbar"><span class="meta tnum">{text.length} / {NOTES_MAX}</span><span class="spacer" />
        <Button size="sm" disabled={!dirty || busy} testid="findings-notes-save" onClick={async () => { setBusy(true); await act('/findings/agent/notes', { notes: text }, { refresh: ['findings-agent'] }); setBusy(false); }}>Save notes</Button></div>
    </div>
  );
}

export async function stopAgent(): Promise<void> {
  if (await confirmDialog('Stop the findings agent?', STOP_TEXT, 'Stop')) await act('/findings/agent/stop', {}, { refresh: [...REFRESH] });
}

function Agent({ fa, nowS, starting, setStarting, openNotes }: { fa: FindingsAgent; nowS: number; starting: boolean; setStarting: (v: boolean) => void; openNotes: () => void }) {
  const v = agentView(fa, nowS);
  if (!v) return null;
  return (
    <div class="fagent col" data-testid="findings-agent">
      <div class="card col">
        <div class="row"><Icon name="findings" size={14} /><b>Findings agent</b><Pill tone={v.tone} dot={v.live ? 'live' : undefined} testid="findings-agent-state">{v.label}</Pill></div>
        <span class="meta">opencode sessions on a free model that keep exploring the committed code, an area at a time: bugs a user can hit (each checked by a second session before it is kept) and the rolling lists (unused files, dead code, code to unify, safety). They write down what each area does for the next pass. Read-only.</span>
        {v.info.length ? <dl class="kv">{v.info.map(([k, x]) => <><dt key={`k${k}`}>{k}</dt><dd key={`v${k}`}>{k === 'session' ? <a href="#" onClick={(e) => { e.preventDefault(); openSession(x, 'Findings agent'); }}>{x}</a> : x}</dd></>)}</dl> : null}
        <div class="toolbar">
          {v.live ? <Button variant="primary" icon="sessions" onClick={() => void watch()}>Watch it work</Button>
            : v.hasPastSession ? <Button icon="sessions" onClick={() => void watch()}>Its last pass</Button> : null}
          {v.live ? <Button icon="stop" testid="findings-agent-stop" onClick={() => void stopAgent()}>Stop</Button>
            : !starting ? <Button variant="primary" icon="play" testid="findings-agent-start" onClick={() => setStarting(true)}>Start…</Button> : null}
          <Button variant="ghost" onClick={openNotes}>What it has learned ({Array.isArray(fa.knowledge) ? fa.knowledge.length : 0})</Button>
          <span class="spacer" />
          <span class="meta">The model is FINDINGS_MODEL in Settings{v.live ? ' (then Stop and Start)' : ''}.</span>
        </div>
      </div>
      {starting && !v.live ? <StartForm fa={fa} onDone={() => setStarting(false)} /> : null}
      {v.counts.length ? (
        <div class="card">
          <table class="ftable tnum" data-testid="findings-agent-counts">
            <thead><tr><th>question</th><th>sessions</th><th>kept</th><th>thrown away</th><th>why</th><th>failed</th><th>notes written</th></tr></thead>
            <tbody>{v.counts.map((c) => <tr key={c.lens}><td>{c.lens}</td><td>{c.calls}</td><td>{c.kept}</td><td>{c.thrown}</td><td class="meta">{c.why}</td><td>{c.failed}</td><td>{c.notes}</td></tr>)}</tbody>
          </table>
        </div>
      ) : null}
      {v.passes.length ? (
        <div class="card col">
          <div class="section-title">Its last sessions</div>
          {v.passes.map((p, i) => (
            <div key={i} class={`list-row fpass${p.session ? '' : ' fpass--static'}`} role={p.session ? 'button' : undefined} tabIndex={p.session ? 0 : undefined}
              onClick={p.session ? () => openSession(p.session, p.label) : undefined}
              onKeyDown={p.session ? (e: KeyboardEvent) => { if (e.key === 'Enter') openSession(p.session, p.label); } : undefined}>
              <Pill tone={p.tone}>{p.outcome}</Pill><span class="ellipsis">{p.label}</span>
              <span class="meta ellipsis">{p.found ? `${p.found} new ` : ''}{p.started}{p.covered ? ` · ${p.covered}` : ''}</span>
            </div>
          ))}
        </div>
      ) : null}
      <Notes fa={fa} />
    </div>
  );
}

// ---- knowledge ---------------------------------------------------------------------------------------------

function NoteReader({ n }: { n: NoteRef }) {
  const [st, setSt] = useState<{ text?: string; err?: { code: string; message: string } } | null>(null);
  const load = () => {
    setSt(null);
    void bridge.api.get<{ text: string }>(api(notePath(n))).then((r) => {
      setSt(r.ok ? { text: typeof r.data?.text === 'string' ? r.data.text : '' } : { err: r.error });
    }, (e) => setSt({ err: { code: 'error', message: String(e) } }));
  };
  useEffect(load, [n.slug, n.name]);
  return (
    <div class="fnote" data-testid="findings-note">
      <div class="row"><h2 class="ellipsis grow">{n.title}</h2><span class="meta">{n.repo}{n.area ? ` · ${n.area}/` : ''}{n.updated ? ` · ${n.updated}` : ''}</span></div>
      {!st ? <Skeleton rows={6} /> : st.err ? <ErrorPanel error={st.err} onRetry={load} />
        : st.text ? <Markdown text={st.text} testid="findings-note-text" /> : <span class="meta">This note is empty.</span>}
    </div>
  );
}

function Knowledge({ fa, pick, setPick }: { fa: FindingsAgent; pick: string; setPick: (k: string) => void }) {
  const [q, setQ] = useState('');
  const groups = useMemo(() => groupKnowledge(fa.knowledge, q), [fa.knowledge, q]);
  const flat = useMemo(() => groups.flatMap((g) => g.notes), [groups]);
  const cur = flat.find((x) => `${x.slug}/${x.name}` === pick) ?? null;
  if (!Array.isArray(fa.knowledge) || !fa.knowledge.length) {
    return <EmptyState icon="file" text="No area notes yet. The findings agent writes down what each area does as it explores." />;
  }
  const onKey = (e: KeyboardEvent) => {
    if (isTyping(e.target) || !flat.length || (e.key !== 'ArrowDown' && e.key !== 'ArrowUp')) return;
    e.preventDefault();
    const i = cur ? flat.indexOf(cur) : -1;
    const n = flat[Math.max(0, Math.min(flat.length - 1, i + (e.key === 'ArrowDown' ? 1 : -1)))];
    setPick(`${n.slug}/${n.name}`);
  };
  return (
    <div class="fsplit">
      <div class="fleft">
        <div class="fbar"><label class="search-field"><Icon name="search" size={13} /><input class="input input--bare" placeholder="Filter notes" aria-label="Filter notes" value={q} onInput={(e) => setQ((e.target as HTMLInputElement).value)} /></label></div>
        <div class="flist" role="listbox" aria-label="Area notes" tabIndex={0} onKeyDown={onKey} data-testid="findings-notes-list">
          {groups.map((g) => (
            <div key={g.repo} role="group" aria-label={g.repo}>
              <div class="section-title fgroup">{g.repo}</div>
              {g.notes.map((x) => {
                const id = `${x.slug}/${x.name}`;
                return (
                  <div key={id} class="list-row" role="option" aria-selected={id === pick} data-testid={`note-${x.slug}-${x.name}`} onClick={() => setPick(id)} title={x.summary}>
                    <span class="ellipsis grow">{x.title}</span><span class="meta">{x.lens}</span>
                  </div>
                );
              })}
            </div>
          ))}
          {!groups.length ? <span class="meta fhint">No note matches.</span> : null}
        </div>
      </div>
      <div class="fright">{cur ? <NoteReader key={pick} n={cur} /> : <EmptyState icon="file" text="Select a note to read it." />}</div>
    </div>
  );
}

// ---- view --------------------------------------------------------------------------------------------------

export default function FindingsView({ ctx }: { ctx: PanelCtx }) {
  const e = useTopic('findings');
  const ae = useTopic('findings-agent');
  const p = ctx.params.value as { tab?: Tab; note?: string };
  const [tab, setTabS] = useState<Tab>(p.tab && ['open', 'archived', 'agent', 'notes'].includes(p.tab) ? p.tab : 'open');
  const [note, setNoteS] = useState(typeof p.note === 'string' ? p.note : '');
  const [q, setQ] = useState(''), [src, setSrc] = useState(''), [cat, setCat] = useState('');
  const [starting, setStarting] = useState(false);
  const setTab = (t: Tab) => { setTabS(t); ctx.update({ tab: t }); };
  const setNote = (n: string) => { setNoteS(n); ctx.update({ note: n }); };
  useEffect(() => { ctx.setTitle('Findings'); }, []);
  const d = e?.data.value;
  const fa = ae?.data.value;
  const b = useMemo(() => board(d?.items), [d]);
  const fc = useMemo(() => facets([...b.open, ...b.archived]), [b]);
  const nowS = Math.floor(Date.now() / 1000);
  const av = useMemo(() => agentView(fa, nowS), [fa, ae?.at.value]);
  useEffect(() => { ctx.setTabState(av?.live ? 'running' : 'idle'); }, [av?.live]);
  const kn = Array.isArray(fa?.knowledge) ? fa!.knowledge.length : 0;
  const listTab = tab === 'open' || tab === 'archived';
  const agentBtn: ComponentChildren = !av ? null : av.live
    ? <Button size="sm" icon="stop" testid="findings-head-stop" onClick={() => void stopAgent()}>Stop agent</Button>
    : <Button size="sm" icon="play" testid="findings-head-start" onClick={() => { setTab('agent'); setStarting(true); }}>Start agent…</Button>;
  const topicState = (x: TopicEntry<unknown> | null, body: () => ComponentChildren, webPath: string) => {
    if (!x) return <Skeleton rows={6} />;
    if (x.data.value !== undefined) return body();
    if (x.unavailable.value) return <EmptyState icon="globe" text="This serve does not offer it to the app yet." action={<Button icon="globe" onClick={() => openWeb(webPath)}>Open in Web panel</Button>} />;
    if (x.error.value) return <ErrorPanel error={x.error.value} onRetry={() => store.load(x.topic)} />;
    return <div data-testid="findings-loading"><Skeleton rows={8} /></div>;
  };
  return (
    <div class="view findings-view" data-testid="view-findings">
      <div class="view-head">
        <h1>Findings</h1>
        {av ? <Pill tone={av.tone} dot={av.live ? 'live' : undefined} testid="findings-head-state">agent {av.label.length > 40 ? 'not answering' : av.label}</Pill> : null}
        {agentBtn}
        <span class="grow" />
        {listTab ? (
          <>
            <label class="search-field"><Icon name="search" size={13} /><input class="input input--bare" placeholder="Filter findings" aria-label="Filter findings" data-testid="findings-filter" value={q} onInput={(ev) => setQ((ev.target as HTMLInputElement).value)} /></label>
            {fc.sources.length > 1 ? <select class="input fselect" aria-label="Source" value={src} onChange={(ev) => setSrc((ev.target as HTMLSelectElement).value)}>
              <option value="">all sources</option>{fc.sources.map((s) => <option key={s} value={s}>{s === 'agent' ? 'findings agent' : s}</option>)}</select> : null}
            {fc.cats.length > 1 ? <select class="input fselect" aria-label="Category" value={cat} onChange={(ev) => setCat((ev.target as HTMLSelectElement).value)}>
              <option value="">all categories</option>{fc.cats.map((s) => <option key={s} value={s}>{s}</option>)}</select> : null}
          </>
        ) : null}
        <AsOf e={tab === 'agent' || tab === 'notes' ? ae : e} />
        <Button size="sm" icon="globe" iconOnly title="Open the web version" onClick={() => openWeb(tab === 'notes' ? WEB.findingsNotes : tab === 'agent' ? WEB.findingsAgent : VIEW_WEB.findings)} />
      </div>
      <Tabs<Tab> value={tab} onChange={setTab} testidPrefix="findings-tab-"
        tabs={[{ id: 'open', label: 'Open', badge: d ? b.openTotal : undefined }, { id: 'archived', label: 'Archived', badge: d ? b.archivedTotal : undefined },
          { id: 'agent', label: 'Agent' }, { id: 'notes', label: 'What it learned', badge: fa ? kn : undefined }]} />
      <div class="view-body view-body--flush findings-body">
        {listTab ? topicState(e, () => <Board key={tab} rows={tab === 'open' ? b.open : b.archived} total={tab === 'open' ? b.openTotal : b.archivedTotal} archived={tab === 'archived'} q={q} src={src} cat={cat} />, VIEW_WEB.findings)
          : topicState(ae, () => (tab === 'agent'
            ? <div class="fpad"><Agent fa={fa!} nowS={nowS} starting={starting} setStarting={setStarting} openNotes={() => setTab('notes')} /></div>
            : <Knowledge fa={fa!} pick={note} setPick={setNote} />), tab === 'agent' ? WEB.findingsAgent : WEB.findingsNotes)}
      </div>
    </div>
  );
}
