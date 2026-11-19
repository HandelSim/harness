// Changes (diff) for a ticket (PLAN W5 step 13): repo + view selectors, file list, unified diff parsed by diff2html's
// parser and rendered as text nodes (no HTML from the server), collapsed files past OPEN_FILES and a per-file line cap
// so a huge diff never freezes the UI, Show more lines (/lines), click-a-line review comments sent with Fix (POST
// /tickets/K/fix, gated by ticket.actions.fix). Used as the ticket panel's Changes tab and as the 'diff' panel kind.
import { Fragment } from 'preact';
import { memo } from 'preact/compat';
import { useCallback, useEffect, useMemo, useRef, useState } from 'preact/hooks';
import type { Diff, Ticket } from '../../../shared/api';
import { act, useTopic } from '../../core/app';
import { api, bridge, enc } from '../../core/bridge';
import { plural } from '../../core/format';
import { openPanel, openWeb, type PanelCtx } from '../../shell/dock';
import { Button, EmptyState, ErrorPanel, GateButton, Pill, Skeleton } from '../../ui/kit';
import { Icon } from '../../ui/Icon';
import {
  anchorKey, commentWhere, fixItems, FILE_LINE_CAP, gapAbove, insertLines, OPEN_FILES, parseDiff, putComment,
  reviewError, rowAnchor, totals, viewTakesComments, type FileView, type ReviewComment, type Row,
} from './diff-model';
import { draftKey, FIX_MAX_CHARS, FIX_MAX_COMMENTS, SOFT_ERRORS } from './model';
import { useJsonDraft } from './parts';

type DiffData = Diff & { kind?: string };
const VIEW_LABEL: Record<string, string> = { work: "The ticket's work", uncommitted: 'Uncommitted', unpushed: 'Not pushed' };

export function ChangesTab({ ticketKey, t, standalone }: { ticketKey: string; t: Ticket | null; standalone?: boolean }) {
  const [sel, setSel] = useState<{ repo: string; view: string }>({ repo: '.', view: '' });
  const [d, setD] = useState<DiffData | null>(null);
  const [err, setErr] = useState<{ code: string; message: string } | null>(null);
  const [loading, setLoading] = useState(true);
  const seq = useRef(0);
  const load = async (repo = sel.repo, view = sel.view) => {
    const n = ++seq.current;
    setLoading(true);
    const q: Record<string, string> = { repo };
    if (view) q.view = view;
    const r = await bridge.api.get<DiffData>(api(`/tickets/${enc(ticketKey)}/diff`), q);
    if (n !== seq.current) return;
    setLoading(false);
    // a failed switch keeps showing the old diff, so the selectors go back to what is shown
    if (!r.ok) { setErr(r.error); const cur = dRef.current; if (cur) setSel({ repo: cur.repo, view: cur.view }); return; }
    setErr(null);
    if (r.data) { setD(r.data); setSel({ repo: r.data.repo, view: r.data.view }); }
  };
  const dRef = useRef<DiffData | null>(null);
  dRef.current = d;
  useEffect(() => { void load(); }, [ticketKey]);

  const [draft, setDraft, clearDraft] = useJsonDraft<{ items: ReviewComment[] }>(draftKey(ticketKey, 'review'), { items: [] });
  const items = Array.isArray(draft.items) ? draft.items : [];
  // stable callbacks over refs, so a memoised FileBlock re-renders only when its own rows or comments change (P2-3)
  const live = useRef({ items, setDraft });
  live.current = { items, setDraft };
  const put = useCallback((c: ReviewComment) => { const l = live.current; l.setDraft({ items: putComment(l.items, c) }); }, []);
  const drop = (i: number) => setDraft({ items: items.filter((_, j) => j !== i) });

  const parsed = useMemo(() => (d ? parseDiff(d.diff || '') : { files: [], cut: false }), [d?.diff]);
  const [rowsOverride, setRowsOverride] = useState<Record<string, Row[]>>({});
  useEffect(() => { setRowsOverride({}); }, [d?.diff, d?.repo, d?.view]);
  const setRows = useCallback((path: string, rows: Row[]) => setRowsOverride((o) => ({ ...o, [path]: rows })), []);
  const byFile = useFileComments(items, d?.repo ?? '.', d?.view ?? '');
  const files = parsed.files;
  const tot = totals(files);
  const canComment = !!d && viewTakesComments(d.view);
  const webPath = `/runs/ticket/${enc(ticketKey)}/diff${d ? `?repo=${enc(d.repo)}&view=${enc(d.view)}` : ''}`;
  const fileRefs = useRef<Record<string, HTMLElement | null>>({});

  const head = (
    <div class="toolbar dv-bar">
      {d && d.repos.length > 1 ? (
        <select class="select" aria-label="Repository" data-testid="diff-repo" value={sel.repo}
          onChange={(e) => { const repo = (e.target as HTMLSelectElement).value; setSel({ repo, view: '' }); void load(repo, ''); }}>
          {d.repos.map((r) => <option key={r.rel} value={r.rel}>{r.label || r.rel}</option>)}
        </select>
      ) : null}
      {d ? (
        <div class="seg" role="group" aria-label="Which changes">
          {d.views.map((v) => (
            <button key={v} type="button" aria-pressed={v === sel.view} data-testid={`diff-view-${v}`}
              onClick={() => { setSel({ repo: sel.repo, view: v }); void load(sel.repo, v); }}>{VIEW_LABEL[v] ?? v}</button>
          ))}
        </div>
      ) : null}
      {d && files.length ? <span class="meta tnum" data-testid="diff-totals">{plural(tot.files, 'file')} <span class="dv-add">+{tot.added}</span> <span class="dv-del">−{tot.deleted}</span></span> : null}
      <span class="spacer" />
      {loading && d ? <span class="spin" aria-label="Loading" /> : null}
      <Button size="sm" variant="ghost" icon="refresh" iconOnly aria-label="Reload" title="Reload" onClick={() => load()} testid="diff-reload" />
      {!standalone ? <Button size="sm" variant="ghost" icon="split" iconOnly aria-label="Open in its own tab" title="Open in its own tab"
        onClick={() => openPanel('diff', ticketKey, { title: `${ticketKey} changes`, params: { key: ticketKey } })} /> : null}
      <Button size="sm" variant="ghost" icon="globe" iconOnly aria-label="Open web version" title="Open web version" onClick={() => openWeb(webPath)} />
    </div>
  );

  if (!d) {
    return (
      <div class="dv" data-testid="diff">
        {head}
        {err ? <ErrorPanel error={err} onRetry={() => load()} /> : <Skeleton rows={8} />}
      </div>
    );
  }
  const notes = [d.error, typeof d.says === 'string' ? d.says : '', typeof d.notes === 'string' ? d.notes : ''].filter(Boolean);
  return (
    <div class="dv" data-testid="diff">
      {head}
      {err ? <div class="inline-err" role="alert">{err.message || err.code}</div> : null}
      {notes.map((n, i) => <div key={i} class={`notice ${d.error && i === 0 ? 'notice--warn' : ''}`}><Icon name="info" size={16} /><div class="tk-pre">{n}</div></div>)}
      {d.commits.length ? (
        <details class="dv-commits">
          <summary class="meta">{plural(d.commits.length, 'commit')}</summary>
          <ul>{d.commits.map((c) => <li key={c.sha}><span class="mono faint">{String(c.sha).slice(0, 10)}</span> {c.subject}</li>)}</ul>
        </details>
      ) : null}
      {parsed.cut ? <div class="notice notice--warn"><Icon name="alert" size={16} /><div>This diff is very large; only its first part is shown. Open the web version for the rest.</div></div> : null}
      <div class="dv-split">
        {files.length > 1 ? (
          <nav class="dv-files" aria-label="Changed files" data-testid="diff-files">
            {files.map((f) => (
              <button key={f.path} type="button" class="list-row dv-file-link" title={f.path}
                onClick={() => { const el = fileRefs.current[f.path]; el?.scrollIntoView({ block: 'start' }); el?.querySelector<HTMLElement>('.dv-file-head')?.focus(); }}>
                <span class={`dv-st dv-st--${f.status}`} aria-label={f.status}>{f.status[0].toUpperCase()}</span>
                <span class="ellipsis grow">{f.path}</span>
                <span class="meta tnum"><span class="dv-add">+{f.added}</span> <span class="dv-del">−{f.deleted}</span></span>
              </button>
            ))}
          </nav>
        ) : null}
        <div class="dv-main">
          {!files.length ? <EmptyState icon="check" text={d.error ? 'No diff to show.' : 'No changes in this view.'} /> : null}
          {files.map((f, i) => (
            <div key={`${d.repo}\n${d.view}\n${f.path}`} ref={(el) => { fileRefs.current[f.path] = el; }}>
              <FileBlock f={f} rows={rowsOverride[f.path] ?? f.rows} startOpen={i < OPEN_FILES} d={d} ticketKey={ticketKey}
                canComment={canComment} items={byFile.get(f.path) ?? NONE} put={put} setRows={setRows} />
            </div>
          ))}
        </div>
      </div>
      <Tray ticketKey={ticketKey} t={t} items={items} canComment={canComment} put={put} drop={drop} clearDraft={clearDraft} />
    </div>
  );
}

const NONE: ReviewComment[] = [];
/** Comments per file path for the shown repo/view; a file's array keeps its identity while its comments do not change. */
function useFileComments(items: ReviewComment[], repo: string, view: string): Map<string, ReviewComment[]> {
  const prev = useRef(new Map<string, ReviewComment[]>());
  return useMemo(() => {
    const m = new Map<string, ReviewComment[]>();
    for (const c of items) {
      if (!c.path || (c.repo || '.') !== repo || (c.view || 'work') !== view) continue;
      const a = m.get(c.path);
      if (a) a.push(c); else m.set(c.path, [c]);
    }
    for (const [k, a] of m) { const o = prev.current.get(k); if (o && o.length === a.length && o.every((x, i) => x === a[i])) m.set(k, o); }
    prev.current = m;
    return m;
  }, [items, repo, view]);
}

/** The whole-change comment and Send; its own component so typing re-renders only the tray (P2-3). */
function Tray({ ticketKey, t, items, canComment, put, drop, clearDraft }: {
  ticketKey: string; t: Ticket | null; items: ReviewComment[]; canComment: boolean;
  put: (c: ReviewComment) => void; drop: (i: number) => void; clearDraft: () => void;
}) {
  const [general, setGeneral] = useState('');
  const [sendErr, setSendErr] = useState('');
  const send = async () => {
    setSendErr('');
    const all = general.trim() ? putComment(items, { text: general.trim() }) : items;
    const why = reviewError(all);
    if (why) { setSendErr(why); return; }
    const r = await act(`/tickets/${enc(ticketKey)}/fix`, { comments: fixItems(all) }, { refresh: [`ticket:${ticketKey}`, 'queue'] });
    if (r.ok) { clearDraft(); setGeneral(''); }
    else if (!SOFT_ERRORS.has(r.error.code)) setSendErr(r.error.message || r.error.code);
  };
  return (
    <section class="dv-tray" aria-label="Review comments" data-testid="diff-tray">
      <div class="section-title">Review comments <span class="badge tnum" data-testid="diff-count">{items.length}/{FIX_MAX_COMMENTS}</span></div>
      {!canComment ? <div class="meta">Comments are taken on the ticket's work and uncommitted views.</div> : <div class="meta">Click a line (or focus the diff and press Enter) to comment on it; Enter on a hunk header shows the lines above. Comments are kept until you send them.</div>}
      {items.length ? (
        <ol class="dv-tray-list">
          {items.map((c, i) => (
            <li key={i} data-testid="diff-comment">
              <span class="mono meta ellipsis" title={commentWhere(c)}>{commentWhere(c)}</span>
              <span class="tk-pre grow">{c.text}</span>
              <Button size="sm" variant="ghost" icon="x" iconOnly aria-label="Remove comment" onClick={() => drop(i)} />
            </li>
          ))}
        </ol>
      ) : null}
      <textarea class="textarea" rows={2} maxLength={FIX_MAX_CHARS} value={general} placeholder="A comment on the whole change"
        aria-label="A comment on the whole change" data-testid="diff-general" onInput={(e) => setGeneral((e.target as HTMLTextAreaElement).value)} />
      {sendErr ? <div class="inline-err" role="alert" data-testid="diff-send-error">{sendErr}</div> : null}
      <div class="row">
        <Button size="sm" disabled={!general.trim()} onClick={() => { put({ text: general.trim() }); setGeneral(''); }}>Add comment</Button>
        <span class="spacer" />
        <GateButton gate={t?.actions.fix} variant="primary" size="sm" icon="send" testid="diff-send"
          disabled={!items.length && !general.trim()} onClick={send}>
          Send {plural(items.length + (general.trim() ? 1 : 0), 'comment')} to the fixer
        </GateButton>
      </div>
    </section>
  );
}

let blockN = 0;
const FileBlock = memo(function FileBlock({ f, rows, startOpen, d, ticketKey, canComment, items, put, setRows }: {
  f: FileView; rows: Row[]; startOpen: boolean; d: DiffData; ticketKey: string; canComment: boolean;
  items: ReviewComment[]; put: (c: ReviewComment) => void; setRows: (path: string, r: Row[]) => void;
}) {
  const [open, setOpen] = useState(startOpen);
  // rows are rendered FILE_LINE_CAP at a time; "Show more" adds MORE_STEP, never the whole file in one commit (P2-3)
  const [limit, setLimit] = useState(FILE_LINE_CAP);
  const [cursor, setCursor] = useState(-1);
  const [editing, setEditing] = useState<number>(-1);
  const [moreErr, setMoreErr] = useState('');
  const uid = useMemo(() => `dv${++blockN}`, []);
  const shown = rows.length > limit ? rows.slice(0, limit) : rows;
  const byAnchor = useMemo(() => {
    const m = new Map<string, ReviewComment>();
    for (const c of items) m.set(anchorKey(c), c);
    return m;
  }, [items]);
  const anchorOf = (r: Row): ReviewComment | null => {
    const a = rowAnchor(r);
    return a ? { text: '', path: f.path, line: a.line, side: a.side, code: r.text, repo: d.repo, view: d.view } : null;
  };
  const startEdit = (i: number) => { if (canComment && rowAnchor(shown[i] ?? { kind: 'hunk', text: '' })) { setCursor(i); setEditing(i); } };
  const more = async (i: number) => {
    const g = gapAbove(rows, i);
    if (!g) return;
    setMoreErr('');
    const r = await bridge.api.get<{ lines: string[]; total: number }>(api(`/tickets/${enc(ticketKey)}/lines`),
      { repo: d.repo, view: d.view, rev: d.rev || '', path: f.path, from: g.from, to: g.to });
    if (!r.ok) { setMoreErr(r.error.message || r.error.code); return; }
    setRows(f.path, insertLines(rows, i, g.from, r.data?.lines ?? []));
  };
  const onKey = (e: KeyboardEvent) => {
    if ((e.target as HTMLElement).closest('textarea')) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp' || e.key === 'j' || e.key === 'k') {
      e.preventDefault();
      setCursor((c) => Math.max(0, Math.min(shown.length - 1, c + (e.key === 'ArrowDown' || e.key === 'j' ? 1 : -1))));
    } else if (e.key === 'Enter' && cursor >= 0) {
      e.preventDefault();
      // a hunk header's Enter is its "N more lines" (the button itself stays out of the tab order)
      if (shown[cursor]?.kind === 'hunk') void more(cursor); else startEdit(cursor);
    }
  };
  useEffect(() => {
    if (cursor < 0) return;
    const el = document.querySelector<HTMLElement>(`[data-dv-file="${CSS.escape(f.path)}"] [data-i="${cursor}"]`);
    el?.scrollIntoView({ block: 'nearest' });
  }, [cursor]);
  return (
    <section class="dv-file" data-dv-file={f.path} data-testid="diff-file">
      <button type="button" class="dv-file-head" aria-expanded={open} onClick={() => setOpen(!open)}>
        <Icon name={open ? 'chevronDown' : 'chevronRight'} size={14} />
        <span class={`dv-st dv-st--${f.status}`}>{f.status}</span>
        <span class="mono ellipsis grow" title={f.status === 'renamed' ? `${f.oldPath} → ${f.path}` : f.path}>
          {f.status === 'renamed' ? `${f.oldPath} → ${f.path}` : f.path}
        </span>
        <span class="meta tnum"><span class="dv-add">+{f.added}</span> <span class="dv-del">−{f.deleted}</span></span>
      </button>
      {open ? (
        f.binary || !rows.length ? <div class="meta dv-none">{f.binary ? 'Binary file' : 'No text changes'}</div> : (
          <div class="dv-body" tabIndex={0} role="listbox" aria-label={`Diff of ${f.path}`} onKeyDown={onKey} data-testid="diff-body"
            aria-activedescendant={cursor >= 0 && cursor < shown.length ? `${uid}-${cursor}` : undefined}>
            {moreErr ? <div class="inline-err" role="alert">{moreErr}</div> : null}
            {shown.map((r, i) => {
              if (r.kind === 'hunk') {
                const g = gapAbove(rows, i);
                return (
                  <div key={`h${i}`} class="dv-hunk" data-i={i} data-cur={cursor === i ? '1' : undefined} id={`${uid}-${i}`}
                    role="option" aria-selected={cursor === i} data-testid="diff-hunk">
                    {g ? <button type="button" class="dv-more" tabIndex={-1} onClick={() => more(i)} title="Show the lines above">
                      <Icon name="chevronDown" size={12} />{g.to - g.from + 1} more lines</button> : null}
                    <span class="mono">{r.text}</span>
                  </div>
                );
              }
              const a = anchorOf(r);
              const c = a ? byAnchor.get(anchorKey(a)) : undefined;
              return (
                <Fragment key={`r${i}`}>
                  <div class={`dv-line dv-line--${r.kind}`} data-i={i} data-cur={cursor === i ? '1' : undefined}
                    data-c={c ? '1' : undefined} role="option" aria-selected={cursor === i} id={`${uid}-${i}`}
                    onClick={() => startEdit(i)} data-testid="diff-line">
                    <span class="dv-n tnum">{r.old ?? ''}</span>
                    <span class="dv-n tnum">{r.new ?? ''}</span>
                    <span class="dv-sign">{r.kind === 'add' ? '+' : r.kind === 'del' ? '−' : ' '}</span>
                    <span class="dv-code">{r.text}</span>
                  </div>
                  {c && editing !== i ? <div class="dv-inline-c tk-pre" onClick={() => startEdit(i)}><Icon name="chat" size={12} /> {c.text}</div> : null}
                  {editing === i && a ? (
                    <CommentBox initial={c?.text ?? ''} where={commentWhere(a)}
                      onSave={(text) => { put({ ...a, text }); setEditing(-1); }}
                      onCancel={() => setEditing(-1)} />
                  ) : null}
                </Fragment>
              );
            })}
            {rows.length > limit ? (
              <button type="button" class="btn btn--sm dv-showall" onClick={() => setLimit(limit + MORE_STEP)} data-testid="diff-show-all">
                Show {Math.min(MORE_STEP, rows.length - limit)} more lines ({limit} of {rows.length} shown)
              </button>
            ) : null}
          </div>
        )
      ) : null}
    </section>
  );
});
const MORE_STEP = 2000;

function CommentBox({ initial, where, onSave, onCancel }: { initial: string; where: string; onSave: (t: string) => void; onCancel: () => void }) {
  const [text, setText] = useState(initial);
  const ta = useRef<HTMLTextAreaElement>(null);
  useEffect(() => { ta.current?.focus(); }, []);
  return (
    <div class="dv-cbox" data-testid="diff-comment-box">
      <div class="meta mono">{where}</div>
      <textarea ref={ta} class="textarea" rows={2} maxLength={FIX_MAX_CHARS} value={text} aria-label={`Comment on ${where}`}
        onInput={(e) => setText((e.target as HTMLTextAreaElement).value)}
        onKeyDown={(e) => {
          if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); onCancel(); }
          else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); onSave(text); }
        }} />
      <div class="row">
        <span class="meta"><kbd>Ctrl</kbd>+<kbd>Enter</kbd> saves · <kbd>Esc</kbd> cancels{initial ? ' · empty removes it' : ''}</span>
        <span class="spacer" />
        <Button size="sm" variant="ghost" onClick={onCancel}>Cancel</Button>
        <Button size="sm" variant="primary" testid="diff-comment-save" disabled={!text.trim() && !initial} onClick={() => onSave(text)}>Save comment</Button>
      </div>
    </div>
  );
}

/** 'diff' panel kind: the Changes view of one ticket in its own tab. */
export default function DiffPanel({ ctx }: { ctx: PanelCtx }) {
  const key = String(ctx.params.value.key ?? ctx.key ?? '');
  const e = useTopic(`ticket:${key}` as `ticket:${string}`);
  const data = e?.data.value;
  const t = data && !('gone' in data) ? data : null;
  useEffect(() => { ctx.setTitle(`${key} changes`); }, [key]);
  return (
    <div class="view tk" data-testid="diff-panel">
      <div class="view-body tk-body tk-body--changes"><ChangesTab ticketKey={key} t={t} standalone /></div>
    </div>
  );
}
