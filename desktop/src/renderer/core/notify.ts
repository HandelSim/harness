// OS notifications (W22, review M4): diff the `queue` topic and the app update state, and show a native
// notification (bridge.notify.show -> Electron Notification in main) only while the window is not focused.
// Per-kind toggles live in settings.notifications. Rate limited: at most LIMIT per WINDOW_MS, several events
// of one kind in one poll collapse into one notification. Pure parts are exported for node tests.
import { effect, type ReadonlySignal } from '@preact/signals-core';
import type { Queue, TicketStatus } from '../../shared/api';
import type { AppUpdateState, Settings } from '../../shared/ipc';

export type NoteKind = 'needsInput' | 'ticketDone' | 'runEnded' | 'update';
export interface Note { kind: NoteKind; id: string; title: string; body: string; route: string }
export interface QueueSnap { status: Map<string, TicketStatus>; running: boolean }

const NEEDS: Partial<Record<TicketStatus, string>> = { 'needs-answers': 'has questions for you', 'plan-review': 'has a plan to review',
  blocked: 'is blocked and needs you' };

export function snapOf(q: Queue): QueueSnap {
  return { status: new Map((q.tickets || []).map((t) => [t.key, t.status])), running: !!q.runner?.running };
}

/** Status transitions worth telling the owner about. The first snapshot (prev null) is a baseline: no events. */
export function queueEvents(prev: QueueSnap | null, q: Queue): Note[] {
  if (!prev) return [];
  const out: Note[] = [];
  for (const t of q.tickets || []) {
    const was = prev.status.get(t.key);
    if (was === t.status) continue;
    const what = NEEDS[t.status];
    const title = t.title ? `${t.key}: ${t.title}` : t.key;
    if (what) out.push({ kind: 'needsInput', id: `${t.key}:${t.status}`, title: `Ticket ${what}`, body: title + (t.reason ? `\n${t.reason}` : ''), route: `ticket:${t.key}` });
    else if (t.status === 'done' && was !== undefined) out.push({ kind: 'ticketDone', id: `${t.key}:done`, title: 'Ticket done', body: title, route: `ticket:${t.key}` });
  }
  if (prev.running && !q.runner?.running) out.push({ kind: 'runEnded', id: 'run-ended', title: 'Queue run ended', body: 'The ticket queue is no longer running.', route: 'view:queue' });
  return out;
}

/** Several notes of one kind collapse into one ("3 tickets need you"). */
export function collapse(notes: Note[]): Note[] {
  const by = new Map<NoteKind, Note[]>();
  for (const n of notes) by.set(n.kind, [...(by.get(n.kind) || []), n]);
  const out: Note[] = [];
  for (const [kind, ns] of by) {
    if (ns.length === 1) { out.push(ns[0]); continue; }
    const title = kind === 'needsInput' ? `${ns.length} tickets need you` : kind === 'ticketDone' ? `${ns.length} tickets done` : ns[0].title;
    out.push({ kind, id: `${kind}:${ns.map((n) => n.id).join(',')}`.slice(0, 190), title, body: ns.map((n) => n.body.split('\n')[0]).join('\n').slice(0, 1500), route: 'view:queue' });
  }
  return out;
}

/** Sliding-window limiter: allow() is true at most `limit` times per `windowMs`. */
export class RateLimit {
  private at: number[] = [];
  constructor(private limit = 4, private windowMs = 60_000) {}
  allow(now: number): boolean {
    this.at = this.at.filter((t) => now - t < this.windowMs);
    if (this.at.length >= this.limit) return false;
    this.at.push(now);
    return true;
  }
}

export interface NotifyDeps {
  queue: ReadonlySignal<Queue | undefined>;
  update: ReadonlySignal<AppUpdateState | null>;
  settings: ReadonlySignal<Settings | null>;
  focused: () => boolean;
  show: (n: { id: string; title: string; body: string; route: string }) => unknown;
  now?: () => number;
}

/** Wire the diffing; returns a disposer. State is diffed even while focused, so returning focus never replays old events. */
export function startNotify(d: NotifyDeps): () => void {
  const rl = new RateLimit();
  const now = d.now || Date.now;
  let snap: QueueSnap | null = null;
  let lastPhase: AppUpdateState['phase'] | null = null;
  const emit = (notes: Note[]) => {
    const s = d.settings.value?.notifications;
    if (!s || d.focused()) return;
    for (const n of collapse(notes.filter((x) => s[x.kind]))) {
      if (!rl.allow(now())) return;
      try { void Promise.resolve(d.show({ id: n.id, title: n.title, body: n.body, route: n.route })).catch(() => {}); } catch { /* bridge gone */ }
    }
  };
  const offQ = effect(() => {
    const q = d.queue.value;
    if (!q || !Array.isArray(q.tickets)) return;
    const notes = queueEvents(snap, q);
    snap = snapOf(q);
    if (notes.length) emit(notes);
  });
  const offU = effect(() => {
    const u = d.update.value;
    if (!u) return;
    const was = lastPhase; lastPhase = u.phase;
    if (u.phase === 'ready' && was !== null && was !== 'ready')
      emit([{ kind: 'update', id: `update:${u.next || ''}`, title: 'Update ready', body: `Harness Desktop ${u.next || ''} is downloaded. Restart to update.`.replace('  ', ' '), route: 'view:updates' }]);
  });
  return () => { offQ(); offU(); };
}

/** notify:click route -> what to open. */
export function parseRoute(route: string): { kind: 'ticket'; key: string } | { kind: 'view'; view: string } | null {
  const m = /^(ticket|view):(.+)$/.exec(route);
  if (!m) return null;
  return m[1] === 'ticket' ? { kind: 'ticket', key: m[2] } : { kind: 'view', view: m[2] };
}
