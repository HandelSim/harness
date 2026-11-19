// Ticket panel tabs that hold user state: Live (stage output tail), Plan (answers form), Notes (composer).
// Live data never clears a draft, steals focus or moves the scroll position.
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'preact/hooks';
import type { ActionResult, Gate, OutChunk, Ticket } from '../../../shared/api';
import { act, store, useTopic } from '../../core/app';
import { api, bridge, enc } from '../../core/bridge';
import { ago, bytes, dateTime, plural } from '../../core/format';
import { Button, EmptyState, GateButton, Pill, Seg } from '../../ui/kit';
import { Icon } from '../../ui/Icon';
import { Markdown } from '../../ui/Markdown';
import { openPanel } from '../../shell/dock';
import {
  appendOut, answerBody, draftKey, emptyTail, liveSession, mergeAnswers, optionLabel, pickOut, splitComments,
  statusLabel, unansweredBlocking, validateComments, FIX_MAX_CHARS, FIX_MAX_COMMENTS, OUT_CAP, SOFT_ERRORS, str,
  type AnswerDraft, type MergeResult, type OutTail, type Question,
} from './model';
import { useDraft, useJsonDraft } from './parts';

export type Run = (action: string, body?: Record<string, unknown>, inline?: boolean) => Promise<ActionResult>;
export const CHANGED_MSG = 'The plan changed while you were answering. It has been reloaded; what you typed is kept and the changed questions are marked.';

// ------------------------------------------------------------------------------------------------ Live
const countLines = (s: string) => { let n = 0; for (let i = 0; i < s.length; i++) if (s.charCodeAt(i) === 10) n++; return n || (s ? 1 : 0); };

export function LiveTab({ t, visible }: { t: Ticket; visible: boolean }) {
  const outs = useMemo(() => [...t.outs].sort((a, b) => b.mtime - a.mtime), [t.outs]);
  const [picked, setPicked] = useState('');                       // '' = follow the newest output
  const auto = pickOut(t.outs, t.state.stage);
  const name = picked && t.outs.some((o) => o.name === picked) ? picked : auto;
  const [tail, setTail] = useState<OutTail>(() => emptyTail(name));
  const tailRef = useRef(tail);
  const [err, setErr] = useState('');
  const box = useRef<HTMLPreElement>(null);
  const [pinned, setPinned] = useState(true);
  const pinnedRef = useRef(true);
  const [fresh, setFresh] = useState(0);

  useEffect(() => {
    if (tailRef.current.name === name) return;
    const z = emptyTail(name);
    tailRef.current = z; setTail(z); setFresh(0); pinnedRef.current = true; setPinned(true);
  }, [name]);

  // Poll every 2 s while the panel is visible; drain up to 8 chunks (256 KiB each) per tick.
  useEffect(() => {
    if (!visible || !name) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      for (let i = 0; i < 8 && alive; i++) {
        const cur = tailRef.current;
        if (cur.name !== name) return;
        const r = await bridge.api.get<OutChunk>(api(`/tickets/${enc(t.key)}/out/${enc(name)}`), { from: cur.next });
        if (!alive) return;
        if (!r.ok) { setErr(r.error.message || r.error.code); break; }
        setErr('');
        const c = r.data;
        if (!c) break;
        const nt = appendOut(tailRef.current, c);
        if (nt !== tailRef.current) {
          tailRef.current = nt; setTail(nt);
          if (!pinnedRef.current && c.text) setFresh((f) => f + countLines(c.text));
        }
        if (c.next >= c.size || !c.text) break;
      }
      if (alive) timer = setTimeout(() => void tick(), 2000);
    };
    void tick();
    return () => { alive = false; if (timer) clearTimeout(timer); };
  }, [visible, name, t.key]);

  useLayoutEffect(() => { const el = box.current; if (el && pinnedRef.current) el.scrollTop = el.scrollHeight; }, [tail.text]);
  const onScroll = () => {
    const el = box.current;
    if (!el) return;
    const at = el.scrollHeight - el.scrollTop - el.clientHeight < 32;
    if (at !== pinnedRef.current) { pinnedRef.current = at; setPinned(at); if (at) setFresh(0); }
  };
  const jump = () => { pinnedRef.current = true; setPinned(true); setFresh(0); const el = box.current; if (el) el.scrollTop = el.scrollHeight; };

  if (!t.outs.length) return <EmptyState icon="terminal" text="No stage output yet. It shows here once a stage starts." />;
  return (
    <div class="tk-live">
      <div class="toolbar tk-live-bar">
        <select class="select" aria-label="Stage output" data-testid="ticket-live-select" value={picked}
          onChange={(e) => setPicked((e.target as HTMLSelectElement).value)}>
          <option value="">Follow latest ({auto})</option>
          {outs.map((o) => <option key={o.name} value={o.name}>{o.name} · {bytes(o.size)}</option>)}
        </select>
        {t.state.status === 'running' ? <Pill tone="ok" dot="live">live</Pill> : null}
        <span class="spacer" />
        {tail.truncated ? <span class="meta">last {bytes(OUT_CAP)} shown</span> : null}
        <span class="meta tnum">{bytes(tail.next)}</span>
        <Button size="sm" variant="ghost" icon="copy" iconOnly aria-label="Copy output" title="Copy output"
          onClick={() => navigator.clipboard?.writeText(tailRef.current.text).catch(() => {})} />
      </div>
      {err ? <div class="inline-err" role="alert">{err}</div> : null}
      <div class="tk-live-wrap">
        <pre ref={box} class="pre tk-live-pre" data-testid="ticket-live-output" tabIndex={0} onScroll={onScroll}>
          {tail.text || <span class="faint">Waiting for output…</span>}
        </pre>
        {!pinned ? (
          <button type="button" class="tk-newpill" data-testid="ticket-live-new" onClick={jump}>
            <Icon name="chevronDown" size={12} />{fresh ? `${fresh} new` : 'Jump to latest'}
          </button>
        ) : null}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------------------------------------ Plan
export function Criteria({ list }: { list: Ticket['state']['criteria'] }) {
  return (
    <ol class="tk-criteria">
      {list.map((c) => (
        <li key={c.id}>
          <span class="tk-cid mono faint">{c.id}</span>
          <div class="grow">
            <div class="tk-pre">{c.text}</div>
            {(['given', 'when', 'then'] as const).some((k) => str(c[k])) ? (
              <div class="tk-gwt meta">
                {(['given', 'when', 'then'] as const).map((k) => (str(c[k]) ? <div key={k}><b>{k}</b> {str(c[k])}</div> : null))}
              </div>
            ) : null}
          </div>
        </li>
      ))}
    </ol>
  );
}

function QuestionCard({ q, value, changed, onChange, disabled }: { q: Question; value: string; changed: boolean; onChange: (v: string) => void; disabled: boolean }) {
  const opts = (q.options ?? []).map(optionLabel).filter((o) => o.label);
  const id = `tkq-${q.id}`;
  return (
    <div class="tk-q" data-changed={changed ? '1' : undefined} data-testid={`ticket-question-${q.id}`}>
      <div class="tk-q-head">
        <span class="tk-cid mono faint">{q.id}</span>
        <label for={id} class="tk-q-text grow">{q.ask || q.text}</label>
        {q.blocking ? <Pill tone="warn">blocking</Pill> : null}
        {changed ? <Pill tone="accent" testid={`ticket-question-changed-${q.id}`}>changed</Pill> : null}
      </div>
      {q.text && q.ask && q.text !== q.ask ? <div class="meta tk-pre">{q.text}</div> : null}
      {opts.length ? (
        <div class="tk-opts" role="group" aria-label="Options">
          {opts.map((o) => (
            <button key={o.value} type="button" class="tk-opt" aria-pressed={value === o.label} disabled={disabled}
              onClick={() => onChange(value === o.label ? '' : o.label)}>{o.label}</button>
          ))}
        </div>
      ) : null}
      <textarea id={id} class="textarea" rows={2} value={value} disabled={disabled}
        placeholder={q.default ? `Default: ${q.default}` : 'Your answer'}
        onInput={(e) => onChange((e.target as HTMLTextAreaElement).value)} />
    </div>
  );
}

/** The plan's text (web parity: serve/runs.py _t_docs reads wiki/notes/tickets/<KEY>-plan.md from the ticket's vault).
 *  Read through GET /vault/file, which serves the active vault: shown only when the ticket ran with that vault (or
 *  names none); a missing file or any error hides the section (the web version still has it). */
export const planRel = (key: string): string => `wiki/notes/tickets/${key}-plan.md`;
export function PlanDoc({ t }: { t: Ticket }) {
  const st = useTopic('status');
  const active = (st?.data.value as { vault?: { dir?: string } | null } | undefined)?.vault?.dir ?? '';
  const own = typeof t.state.vault === 'string' ? t.state.vault : '';
  const rel = planRel(t.key);
  const ok = !!active && (!own || own.replace(/[\\/]+$/, '') === active.replace(/[\\/]+$/, ''));
  const [text, setText] = useState('');
  useEffect(() => {
    let live = true;
    if (!ok) { setText(''); return; }
    void bridge.api.get<{ kind?: string; text?: string }>(api('/vault/file'), { path: rel }).then((r) => {
      if (live) setText(r.ok && r.data?.kind === 'markdown' ? String(r.data.text ?? '') : '');
    });
    return () => { live = false; };
  }, [rel, ok, t.state.plan_sig]);
  if (!text.trim()) return null;
  return (
    <details class="tk-section" open data-testid="ticket-plan-doc">
      <summary class="section-title">The plan
        <Button size="sm" variant="ghost" icon="folder" onClick={(e: Event) => { e.preventDefault(); openPanel('vault-file', rel, { title: `${t.key}-plan.md`, params: { path: rel } }); }}>Open in the vault</Button>
      </summary>
      <Markdown text={text} />
    </details>
  );
}

export function PlanTab({ t, run, approve, notice, setNotice }: {
  t: Ticket; run: Run; approve: () => Promise<unknown>; notice: string; setNotice: (s: string) => void;
}) {
  const s = t.state;
  const qs = s.questions ?? [];
  const [stored, setDraft, clearDraft] = useJsonDraft<AnswerDraft>(draftKey(t.key, 'answers'), { answers: {}, feedback: '' });
  const draft: AnswerDraft = {
    answers: stored.answers && typeof stored.answers === 'object' ? stored.answers : {},
    feedback: typeof stored.feedback === 'string' ? stored.feedback : '',
  };
  const basis = useRef<{ sig: string; qs: Question[] } | null>(null);
  const [changed, setChanged] = useState<string[]>([]);
  const [orphans, setOrphans] = useState<MergeResult['orphans']>([]);
  const [err, setErr] = useState('');
  // A new plan_sig (live update or the reload after a 409 'changed'): keep the draft as is, mark what changed.
  useEffect(() => {
    const b = basis.current;
    basis.current = { sig: s.plan_sig, qs };
    if (!b || b.sig === s.plan_sig) return;
    const m = mergeAnswers(b.qs, draft, qs);
    setChanged(m.changed);
    setOrphans(m.orphans);
  }, [s.plan_sig]);

  const review = s.status === 'plan-review';
  const asking = s.status === 'needs-answers';
  const setAnswer = (id: string, v: string) => setDraft({ ...draft, answers: { ...draft.answers, [id]: v } });
  const send = async (defaults = false) => {
    setErr(''); setNotice('');
    const r = await run('answer', answerBody(qs, draft, s.plan_sig, { defaults }), true);
    if (r.ok) { clearDraft(); setChanged([]); setOrphans([]); return; }
    if (r.error.code === 'changed') { setNotice(r.error.message || CHANGED_MSG); void store.load(`ticket:${t.key}`); }
    else if (!SOFT_ERRORS.has(r.error.code)) setErr(r.error.message || r.error.code);
  };
  const missing = unansweredBlocking(qs, draft);
  const assumptions = Array.isArray(s.assumptions) ? (s.assumptions as { id?: unknown; text?: unknown }[]) : [];

  if (!review && !asking && !qs.length && !s.criteria?.length) {
    return <><EmptyState icon="check" text="Nothing to answer or review right now." /><PlanDoc t={t} /></>;
  }
  return (
    <div class="tk-plan" data-testid="ticket-plan">
      {notice ? (
        <div class="notice notice--warn" role="alert" data-testid="ticket-plan-changed">
          <Icon name="alert" size={16} /><div class="grow">{notice}</div>
          <Button size="sm" variant="ghost" icon="x" iconOnly aria-label="Dismiss" onClick={() => setNotice('')} />
        </div>
      ) : null}
      {!review && !asking ? <div class="notice"><Icon name="info" size={16} /><div>This ticket is {statusLabel(s.status).toLowerCase()}; the plan below is for reference.</div></div> : null}
      <PlanDoc t={t} />
      {s.criteria?.length ? (
        <section class="tk-section">
          <div class="section-title">Acceptance criteria <span class="badge">{s.criteria.length}</span></div>
          <Criteria list={s.criteria} />
        </section>
      ) : null}
      {assumptions.length ? (
        <section class="tk-section">
          <div class="section-title">Assumptions</div>
          <ul class="tk-list">{assumptions.map((a, i) => <li key={i}><span class="tk-cid mono faint">{str(a.id)}</span> <span class="tk-pre">{str(a.text)}</span></li>)}</ul>
        </section>
      ) : null}
      {qs.length ? (
        <section class="tk-section">
          <div class="section-title">Questions <span class="badge">{qs.length}</span></div>
          {qs.map((q) => (
            <QuestionCard key={q.id} q={q} value={draft.answers[q.id] ?? ''} changed={changed.includes(q.id)}
              disabled={!t.actions.answer?.ok} onChange={(v) => setAnswer(q.id, v)} />
          ))}
        </section>
      ) : null}
      {orphans.length ? (
        <div class="notice notice--warn" data-testid="ticket-plan-orphans">
          <Icon name="alert" size={16} />
          <div class="grow">
            <div>{plural(orphans.length, 'answer')} for questions no longer in the plan (kept here so nothing is lost):</div>
            {orphans.map((o) => <div key={o.id} class="tk-orphan"><b>{o.text}</b><div class="tk-pre">{o.answer}</div></div>)}
          </div>
        </div>
      ) : null}
      {asking || review ? (
        <section class="tk-section">
          <label class="section-title" for="tk-feedback">{review ? 'Request changes' : 'Anything else (optional)'}</label>
          <textarea id="tk-feedback" class="textarea" rows={3} data-testid="ticket-plan-feedback" value={draft.feedback}
            disabled={!t.actions.answer?.ok}
            placeholder={review ? 'What should change in the plan?' : 'Extra context for the agent'}
            onInput={(e) => setDraft({ ...draft, feedback: (e.target as HTMLTextAreaElement).value })} />
        </section>
      ) : null}
      {err ? <div class="inline-err" role="alert" data-testid="ticket-plan-error">{err}</div> : null}
      {asking || review ? (
        <div class="tk-plan-actions">
          {missing.length ? <span class="meta">{plural(missing.length, 'blocking question')} without an answer or default</span> : null}
          <span class="spacer" />
          {qs.length ? <GateButton gate={t.actions.answer} testid="ticket-plan-defaults" onClick={() => send(true)}>Take defaults</GateButton> : null}
          {review ? (
            <GateButton gate={t.actions.answer} testid="ticket-plan-changes" disabled={!draft.feedback.trim()}
              title="Write what should change first" onClick={() => send(false)}>Request changes</GateButton>
          ) : null}
          {qs.length && !review ? <GateButton gate={t.actions.answer} variant="primary" icon="send" testid="ticket-plan-send" onClick={() => send(false)}>Send answers</GateButton> : null}
          {review && qs.length ? <GateButton gate={t.actions.answer} icon="send" testid="ticket-plan-send" onClick={() => send(false)}>Send answers</GateButton> : null}
          {review ? <GateButton gate={t.actions.approve} variant="primary" icon="check" testid="ticket-plan-approve" onClick={approve}>Approve plan</GateButton> : null}
        </div>
      ) : null}
    </div>
  );
}

// ------------------------------------------------------------------------------------------------ Notes
export type ComposeMode = 'note' | 'followup' | 'agent' | 'fix';
const MODES: { value: ComposeMode; label: string }[] = [
  { value: 'note', label: 'Note' }, { value: 'followup', label: 'Follow-up' }, { value: 'agent', label: 'Tell the agent' }, { value: 'fix', label: 'Fix comments' },
];

export function NotesTab({ t, run, mode, setMode, focusTick }: { t: Ticket; run: Run; mode: ComposeMode; setMode: (m: ComposeMode) => void; focusTick: number }) {
  const notes = t.state.notes ?? [];
  const reviews = Array.isArray(t.state.owner_reviews) ? (t.state.owner_reviews as { at?: unknown; items?: unknown }[]) : [];
  const live = liveSession(t);
  const se = useTopic(live ? (`session:${live.sid}` as `session:${string}`) : null);
  const sd = se?.data.value;
  const head = sd && !('gone' in sd) ? sd : null;
  const [text, setText, clear] = useDraft(draftKey(t.key, `compose-${mode}`));
  const [err, setErr] = useState('');
  const ta = useRef<HTMLTextAreaElement>(null);
  const lock = useRef(false);
  const list = useRef<HTMLDivElement>(null);
  useEffect(() => { if (focusTick) ta.current?.focus(); }, [focusTick]);
  useEffect(() => { setErr(''); }, [mode]);

  const comments = mode === 'fix' ? splitComments(text) : [];
  const fixErr = mode === 'fix' && text.trim() ? validateComments(comments) : '';
  const gate: Gate | undefined = mode === 'note' ? t.actions.note : mode === 'followup' ? t.actions.followup : mode === 'fix' ? t.actions.fix
    : head ? head.send : { ok: false, reason: live ? 'the agent session is not reachable' : 'no agent session for this ticket' };
  const hint = mode === 'note' ? 'Adds a note to the ticket.'
    : mode === 'followup' ? 'Sends the ticket back for another round with these instructions.'
      : mode === 'fix' ? `Review comments for a fix round. A blank line separates comments (at most ${FIX_MAX_COMMENTS}, ${FIX_MAX_CHARS} characters each).`
        : live ? `Sends a message to the ${live.label || live.stage} agent.` : 'No agent session for this ticket.';

  const send = async () => {
    if (lock.current || !text.trim() || !gate?.ok || fixErr) return;
    lock.current = true;
    setErr('');
    try {
      let r: ActionResult;
      if (mode === 'agent') {
        if (!live) return;
        r = await act(`/sessions/${enc(live.sid)}/send`, { text }, { refresh: [`session:${live.sid}`, `ticket:${t.key}`] });
      } else if (mode === 'fix') {
        const v = validateComments(comments);
        if (v) { setErr(v); return; }
        r = await run('fix', { comments }, true);
      } else r = await run(mode, { text: text.trim() }, true);
      if (r.ok) clear();
      else if (!SOFT_ERRORS.has(r.error.code)) setErr(r.error.message || r.error.code);
    } finally { lock.current = false; }
  };

  return (
    <div class="tk-notes">
      <div class="tk-notes-list" ref={list}>
        {notes.length ? notes.map((n, i) => (
          <div class="tk-note" key={i} data-testid="ticket-note">
            <div class="meta tnum" title={dateTime(n.at)}>{ago(n.at)}</div>
            <div class="tk-pre">{n.text}</div>
          </div>
        )) : <p class="muted small tk-none">No notes yet.</p>}
        {reviews.length ? (
          <section class="tk-section">
            <div class="section-title">Review comments sent</div>
            {reviews.map((r, i) => (
              <div class="tk-note" key={`r${i}`}>
                <div class="meta tnum" title={dateTime(r.at)}>{ago(r.at)} · {plural(Array.isArray(r.items) ? r.items.length : 0, 'comment')}</div>
                <ul class="tk-list">{(Array.isArray(r.items) ? r.items : []).map((x, j) => <li key={j} class="tk-pre">{str(x)}</li>)}</ul>
              </div>
            ))}
          </section>
        ) : null}
      </div>
      <div class="tk-composer">
        <Seg options={MODES} value={mode} onChange={setMode} label="What to send" />
        <div class="meta">{hint}</div>
        <textarea ref={ta} class="textarea" rows={3} data-testid="composer-input" value={text}
          aria-label={MODES.find((m) => m.value === mode)?.label}
          placeholder={mode === 'fix' ? 'First comment\n\nSecond comment' : 'Write…'}
          onInput={(e) => setText((e.target as HTMLTextAreaElement).value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); void send(); }
          }} />
        {mode === 'fix' ? <div class="meta tnum" data-testid="composer-count">{comments.length}/{FIX_MAX_COMMENTS} comments</div> : null}
        {err || fixErr ? <div class="inline-err" role="alert" data-testid="composer-error">{err || fixErr}</div> : null}
        <div class="row tk-composer-foot">
          <span class="meta"><kbd>Enter</kbd> sends · <kbd>Shift</kbd>+<kbd>Enter</kbd> new line</span>
          <span class="spacer" />
          <GateButton gate={gate} variant="primary" size="sm" icon="send" testid="composer-send"
            disabled={!text.trim() || !!fixErr} onClick={send}>Send</GateButton>
        </div>
      </div>
    </div>
  );
}
