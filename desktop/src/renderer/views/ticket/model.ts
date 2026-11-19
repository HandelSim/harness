// Ticket detail model (PLAN W5 steps 7-15, 3.5). Pure; unit-tested in model.test.ts.
import type { OutChunk, Ticket, TicketAction, TicketState, TicketStatus } from '../../../shared/api';

export type Tone = 'ok' | 'warn' | 'err' | 'info' | 'accent' | 'default';
export type TicketTab = 'overview' | 'live' | 'plan' | 'notes' | 'sessions' | 'changes';
export const TICKET_TABS: TicketTab[] = ['overview', 'live', 'plan', 'notes', 'sessions', 'changes'];

export const STATUS_LABEL: Record<TicketStatus, string> = {
  queued: 'Queued', running: 'Running', interrupted: 'Interrupted', 'needs-answers': 'Needs answers',
  'plan-review': 'Plan review', blocked: 'Blocked', stopped: 'Stopped', done: 'Done',
};
export function statusTone(s: TicketStatus | string): Tone {
  switch (s) {
    case 'running': return 'ok';
    case 'done': return 'ok';
    case 'needs-answers': case 'plan-review': return 'accent';
    case 'blocked': return 'err';
    case 'interrupted': case 'stopped': return 'warn';
    case 'queued': return 'info';
    default: return 'default';
  }
}
export function statusLabel(s: string): string { return (STATUS_LABEL as Record<string, string>)[s] ?? s; }

/** Tab opened first for a status (needs input -> plan; running -> live; else overview). */
export function defaultTab(s: TicketStatus | string): TicketTab {
  if (s === 'needs-answers' || s === 'plan-review') return 'plan';
  if (s === 'running') return 'live';
  return 'overview';
}

/** Actions bar order. Each button's enabled state comes from ticket.actions[name] only. */
export const BAR_ACTIONS: TicketAction[] = ['approve', 'continue', 'retry', 'followup', 'stop', 'edit', 'archive', 'unarchive', 'remove'];
export const ACTION_LABEL: Record<TicketAction, string> = {
  answer: 'Send answers', approve: 'Approve plan', retry: 'Retry', continue: 'Continue', stop: 'Stop', remove: 'Remove',
  move: 'Move', note: 'Add note', followup: 'Follow-up', edit: 'Edit', fix: 'Fix these', archive: 'Archive', unarchive: 'Unarchive',
};
/**
 * Which archive toggle to show. Both gates come from the server; this only picks the one that makes sense to
 * display (the archived flag is the server's, not derived), so a disabled twin does not clutter the bar.
 */
export function visibleBarActions(t: Pick<Ticket, 'archived' | 'actions'>): TicketAction[] {
  return BAR_ACTIONS.filter((a) => {
    if (a === 'archive') return !t.archived;
    if (a === 'unarchive') return t.archived;
    if (a === 'approve') return !!t.actions.approve?.ok; // only meaningful in plan review; hidden otherwise
    return true;
  });
}

/** `stopping` is a pass-through key of ticket_state (present while a stop is pending). */
export function isStopping(s: TicketState): boolean { return s.stopping === true || s.stopping === 1 || s.stopping === '1'; }

// ---------------------------------------------------------------------------------------------- answers
export type Question = TicketState['questions'][number];
export interface AnswerDraft { answers: Record<string, string>; feedback: string }
export interface MergeResult {
  draft: AnswerDraft;
  /** Questions whose text/ask/options/default differ from the version the user was answering. */
  changed: string[];
  /** Typed answers whose question no longer exists (shown so nothing typed is lost). */
  orphans: { id: string; text: string; answer: string }[];
}
function qsig(q: Question): string { return JSON.stringify([q.text, q.ask, q.default, q.options, q.blocking]); }

/**
 * Plan changed (409 'changed' or a new plan_sig arrived): keep every typed answer whose question still exists,
 * mark changed questions, and keep answers to removed questions as orphans.
 */
export function mergeAnswers(prev: Question[], draft: AnswerDraft, next: Question[]): MergeResult {
  const before = new Map(prev.map((q) => [q.id, q]));
  const nextIds = new Set(next.map((q) => q.id));
  const answers: Record<string, string> = {};
  const changed: string[] = [];
  for (const q of next) {
    const typed = draft.answers[q.id];
    if (typed !== undefined) answers[q.id] = typed;
    const old = before.get(q.id);
    if (!old || qsig(old) !== qsig(q)) changed.push(q.id);
  }
  const orphans: MergeResult['orphans'] = [];
  for (const [id, answer] of Object.entries(draft.answers)) {
    if (!nextIds.has(id) && answer.trim()) orphans.push({ id, text: before.get(id)?.text ?? id, answer });
  }
  return { draft: { answers, feedback: draft.feedback }, changed: prev.length ? changed : [], orphans };
}

/** POST /tickets/K/answer body. Only current questions with typed text; sig is the plan the user saw. */
export function answerBody(questions: Question[], draft: AnswerDraft, sig: string,
  opts: { defaults?: boolean; go?: boolean } = {}): Record<string, unknown> {
  const answers: Record<string, string> = {};
  for (const q of questions) {
    const a = draft.answers[q.id];
    if (a !== undefined && a.trim()) answers[q.id] = a.trim();
  }
  const body: Record<string, unknown> = { answers, sig };
  if (draft.feedback.trim()) body.feedback = draft.feedback.trim();
  if (opts.defaults) body.defaults = true;
  if (opts.go) body.go = true;
  return body;
}
/** Blocking questions without an answer and without a default (the UI warns; the server decides). */
export function unansweredBlocking(questions: Question[], draft: AnswerDraft): string[] {
  return questions.filter((q) => q.blocking && !(draft.answers[q.id] ?? '').trim() && !String(q.default ?? '').trim()).map((q) => q.id);
}
export function optionLabel(o: unknown): { value: string; label: string } {
  if (o && typeof o === 'object') {
    const r = o as Record<string, unknown>;
    const v = String(r.value ?? r.id ?? r.label ?? r.text ?? '');
    return { value: v, label: String(r.label ?? r.text ?? v) };
  }
  return { value: String(o ?? ''), label: String(o ?? '') };
}

// ---------------------------------------------------------------------------------------------- fix comments
export const FIX_MAX_COMMENTS = 40;
export const FIX_MAX_CHARS = 2000;
export interface FixComment { text: string; path?: string; line?: number; side?: string; code?: string; at?: string; repo?: string }
/** Client-side twin of the server cap (40 x 2000 chars). Returns '' when sendable. */
export function validateComments(cs: FixComment[]): string {
  if (!cs.length) return 'Add at least one comment.';
  if (cs.length > FIX_MAX_COMMENTS) return `At most ${FIX_MAX_COMMENTS} comments (${cs.length} now).`;
  const i = cs.findIndex((c) => c.text.length > FIX_MAX_CHARS);
  if (i >= 0) return `Comment ${i + 1} is ${cs[i].text.length} characters; the limit is ${FIX_MAX_CHARS}.`;
  const e = cs.findIndex((c) => !c.text.trim());
  if (e >= 0) return `Comment ${e + 1} is empty.`;
  return '';
}
/** Add a comment only while under the cap; returns the same array when refused. */
export function addComment(cs: FixComment[], c: FixComment): FixComment[] {
  if (cs.length >= FIX_MAX_COMMENTS) return cs;
  return [...cs, { ...c, text: c.text.slice(0, FIX_MAX_CHARS) }];
}

// ---------------------------------------------------------------------------------------------- live output
export interface OutTail { name: string; next: number; text: string; truncated: boolean }
export const OUT_CAP = 512 * 1024;
export function emptyTail(name: string): OutTail { return { name, next: 0, text: '', truncated: false }; }
/** Append a /out chunk. reset (file got shorter) restarts; the kept text is capped, newest kept. */
export function appendOut(t: OutTail, c: OutChunk, cap = OUT_CAP): OutTail {
  if (c.name && c.name !== t.name) return t;            // stale response for a previously selected file
  if (!c.reset && c.from !== t.next) return t;          // out-of-order or duplicate chunk
  let text = c.reset ? c.text : t.text + c.text;
  let truncated = c.reset ? false : t.truncated;
  if (text.length > cap) { text = text.slice(text.length - cap); truncated = true; }
  return { name: t.name, next: c.next, text, truncated };
}
/** Which out file to tail: the newest one for the current stage, else the newest overall. */
export function pickOut(outs: Ticket['outs'], stage: string): string {
  if (!outs.length) return '';
  const sorted = [...outs].sort((a, b) => b.mtime - a.mtime);
  const st = stage ? sorted.find((o) => o.name === stage || o.name.startsWith(`${stage}-`) || o.name.startsWith(`${stage}.`)) : undefined;
  return (st ?? sorted[0]).name;
}

// ---------------------------------------------------------------------------------------------- timeline
export type StepState = 'done' | 'failed' | 'active' | 'pending';
export interface Step { stage: string; state: StepState; attempts: number }
const FAIL_RE = /fail|error|block|refus|stop|abort/i;
/** Stage timeline from history (order of first appearance), the current stage last result decides its state. */
export function timeline(s: Pick<TicketState, 'history' | 'stage' | 'status'>): Step[] {
  const map = new Map<string, Step>();
  for (const h of s.history ?? []) {
    if (!h.stage) continue;
    const st = map.get(h.stage) ?? { stage: h.stage, state: 'done' as StepState, attempts: 0 };
    st.attempts++;
    st.state = FAIL_RE.test(h.result ?? '') ? 'failed' : 'done';
    map.set(h.stage, st);
  }
  if (s.stage) {
    const cur = map.get(s.stage) ?? { stage: s.stage, state: 'pending' as StepState, attempts: 0 };
    if (s.status === 'running') cur.state = 'active';
    else if (!map.has(s.stage)) cur.state = s.status === 'done' ? 'done' : s.status === 'blocked' ? 'failed' : 'pending';
    map.delete(s.stage);
    map.set(s.stage, cur);
  }
  return [...map.values()];
}

/** Sessions sorted newest first; the working one for the current stage is the live target. */
export function liveSession(t: Pick<Ticket, 'sessions' | 'state'>): Ticket['sessions'][number] | null {
  const list = [...(t.sessions ?? [])].sort((a, b) => b.created - a.created);
  return list.find((x) => x.working && (!t.state.stage || x.stage === t.state.stage)) ?? list.find((x) => x.working)
    ?? list.find((x) => x.stage === t.state.stage) ?? null;
}

/** Draft key inside AppState.drafts ("<scope>:<id>:<field>"). */
export function draftKey(key: string, field: string): string { return `ticket:${key}:${field}`; }

/** Fix composer text -> comments: a blank line separates comments. */
export function splitComments(text: string): FixComment[] {
  return text.split(/\n[ \t]*\n/).map((s) => s.trim()).filter(Boolean).map((t) => ({ text: t }));
}

/** act() already toasts these (result unknown, state refreshed); the panel does not repeat them inline. */
export const SOFT_ERRORS = new Set(['unknown-result', 'timeout', 'offline']);

/** Read a pass-through ticket_state value safely (server strings only ever rendered as text). */
export function rec(v: unknown): Record<string, unknown> | null { return v && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : null; }
export function str(v: unknown): string { return typeof v === 'string' ? v : typeof v === 'number' ? String(v) : ''; }
/** "3 files, +10 -2" from a diffstat object, '' when absent or empty. */
export function diffstatText(v: unknown): string {
  const d = rec(v);
  if (!d) return '';
  const n = (k: string) => (typeof d[k] === 'number' ? d[k] as number : 0);
  if (!n('files') && !n('insertions') && !n('deletions')) return '';
  return `${n('files')} file${n('files') === 1 ? '' : 's'}, +${n('insertions')} −${n('deletions')}`;
}
/** Only http(s) links are ever opened. */
export function safeUrl(v: unknown): string { const s = str(v); return /^https?:\/\//i.test(s) ? s : ''; }
