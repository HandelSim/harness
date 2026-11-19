// Queue view model (W5, 3.4). Pure; unit-tested. Display only: every enable/disable decision comes from the
// server's Gate values (ticket.actions, queue.start); this module maps status x runner to what a row shows.
import type { Queue, Runner, TicketStatus, TicketSummary } from '../../../shared/api';

export type Tone = 'ok' | 'warn' | 'err' | 'info' | 'accent' | 'default';
export interface RowPill { label: string; tone: Tone; dot: 'live' | 'ok' | 'warn' | 'err' | null; live: boolean; attention: boolean; hint: string }

/** Ticket keys the runner is working on now: the main ticket plus parallel slots (`on[]`). */
export function liveKeys(r: Runner | null | undefined): Set<string> {
  const s = new Set<string>();
  if (!r || !(r.running || r.halting)) return s;
  if (typeof r.ticket === 'string' && r.ticket) s.add(r.ticket);
  for (const x of Array.isArray(r.on) ? r.on : []) {
    if (typeof x === 'string' && x) s.add(x);
    else if (x && typeof x === 'object') {
      const k = (x as { ticket?: unknown; key?: unknown }).ticket ?? (x as { key?: unknown }).key;
      if (typeof k === 'string' && k) s.add(k);
    }
  }
  return s;
}

/** Stage of a parallel slot for a key, if the runner reports one. */
export function slotStage(r: Runner | null | undefined, key: string): string {
  if (!r) return '';
  if (r.ticket === key) return r.stage || '';
  for (const x of Array.isArray(r.on) ? r.on : []) {
    if (x && typeof x === 'object' && ((x as { ticket?: unknown }).ticket === key || (x as { key?: unknown }).key === key)) {
      const st = (x as { stage?: unknown }).stage;
      return typeof st === 'string' ? st : '';
    }
  }
  return '';
}

const BASE: Record<TicketStatus, { label: string; tone: Tone }> = {
  queued: { label: 'queued', tone: 'default' },
  running: { label: 'running', tone: 'accent' },
  interrupted: { label: 'interrupted', tone: 'warn' },
  'needs-answers': { label: 'needs answers', tone: 'accent' },
  'plan-review': { label: 'plan review', tone: 'accent' },
  blocked: { label: 'blocked', tone: 'err' },
  stopped: { label: 'stopped', tone: 'default' },
  done: { label: 'done', tone: 'ok' },
};

/** The status x runner matrix: what a ticket row's pill says. */
export function rowPill(t: Pick<TicketSummary, 'key' | 'status' | 'stopping' | 'stage'>, r: Runner | null | undefined): RowPill {
  const base = BASE[t.status] ?? { label: String(t.status || 'unknown'), tone: 'default' as Tone };
  const live = liveKeys(r).has(t.key);
  const attention = t.status === 'needs-answers' || t.status === 'plan-review';
  if (t.stopping) return { label: 'stopping…', tone: 'warn', dot: 'live', live, attention: false, hint: 'the stage is being stopped' };
  switch (t.status) {
    case 'running':
      if (live && r?.halting) return { label: 'halting', tone: 'warn', dot: 'live', live, attention, hint: 'the run halts after this stage' };
      if (live) return { label: 'running', tone: 'accent', dot: 'live', live, attention, hint: '' };
      if (r?.starting) return { label: 'running', tone: 'info', dot: 'warn', live, attention, hint: 'the runner is starting' };
      return { label: 'running', tone: 'warn', dot: 'warn', live, attention, hint: 'no runner is working on it; it resumes on the next start' };
    case 'queued':
      return { ...base, dot: null, live, attention, hint: r?.running || r?.starting ? '' : 'start the queue to run it' };
    case 'interrupted':
      return { ...base, dot: 'warn', live, attention, hint: 'resumes where it stopped on the next start' };
    case 'needs-answers': case 'plan-review':
      return { ...base, dot: live ? 'live' : null, live, attention, hint: 'waiting for you' };
    case 'blocked':
      return { ...base, dot: 'err', live, attention, hint: '' };
    case 'done':
      return { ...base, dot: null, live, attention, hint: '' };
    default:
      return { ...base, dot: null, live, attention, hint: '' };
  }
}

export type RunnerPhase = 'idle' | 'starting' | 'running' | 'halting' | 'exited';
export interface RunnerCard { phase: RunnerPhase; title: string; detail: string; mode: string; slots: number; busy: number; tone: Tone }
export function runnerCard(r: Runner | null | undefined): RunnerCard {
  const mode = !r ? '' : r.single ? 'single message' : r.host ? 'host' : 'container';
  const slots = r && typeof r.parallel === 'number' && r.parallel > 0 ? r.parallel : 1;
  const busy = liveKeys(r).size;
  if (!r) return { phase: 'idle', title: 'Queue idle', detail: '', mode, slots, busy, tone: 'default' };
  const where = [r.ticket, r.stage, r.step].filter((x) => typeof x === 'string' && x).join(' · ');
  if (r.halting) return { phase: 'halting', title: 'Halting after this stage', detail: where, mode, slots, busy, tone: 'warn' };
  if (r.starting) return { phase: 'starting', title: 'Starting…', detail: where, mode, slots, busy, tone: 'info' };
  if (r.running) return { phase: 'running', title: r.ticket ? `Running ${r.ticket}` : 'Running', detail: [r.stage, r.step].filter(Boolean).join(' · '), mode, slots, busy, tone: 'accent' };
  if (r.exited !== null && r.exited !== undefined && r.exited !== false && r.exited !== '') {
    const e = r.exited as { code?: unknown; reason?: unknown } | string | number | true;
    const detail = typeof e === 'object' ? [e.code !== undefined ? `code ${String(e.code)}` : '', typeof e.reason === 'string' ? e.reason : ''].filter(Boolean).join(' · ')
      : e === true ? '' : String(e);
    return { phase: 'exited', title: 'Last run ended', detail, mode, slots, busy, tone: 'default' };
  }
  return { phase: 'idle', title: 'Queue idle', detail: '', mode, slots, busy, tone: 'default' };
}

export interface Counts { total: number; attention: number; running: number; queued: number; blocked: number; done: number }
export function counts(list: TicketSummary[]): Counts {
  const c: Counts = { total: list.length, attention: 0, running: 0, queued: 0, blocked: 0, done: 0 };
  for (const t of list) {
    if (t.status === 'needs-answers' || t.status === 'plan-review') c.attention++;
    else if (t.status === 'running') c.running++;
    else if (t.status === 'queued' || t.status === 'interrupted') c.queued++;
    else if (t.status === 'blocked') c.blocked++;
    else if (t.status === 'done') c.done++;
  }
  return c;
}

/** Case-insensitive filter over key, title, project, branch, status, stage. */
export function filterTickets(list: TicketSummary[], q: string): TicketSummary[] {
  const s = q.trim().toLowerCase();
  if (!s) return list;
  const terms = s.split(/\s+/);
  return list.filter((t) => {
    const hay = `${t.key} ${t.title} ${t.project} ${t.branch} ${t.status} ${t.stage}`.toLowerCase();
    return terms.every((x) => hay.includes(x));
  });
}

/**
 * Optimistic reorder: while a move is in flight the list shows the local order. The pending order is dropped
 * once the server's order matches it, or after `ttlMs` (the server wins), or when the key disappears.
 */
export interface PendingMove { key: string; order: string[]; at: number }
export function moveLocal(order: string[], key: string, before: string | null): string[] {
  const rest = order.filter((k) => k !== key);
  if (!order.includes(key)) return order;
  if (before === null) return [...rest, key];
  const i = rest.indexOf(before);
  if (i < 0) return order;
  return [...rest.slice(0, i), key, ...rest.slice(i)];
}
export function reconcile(server: string[], pending: PendingMove | null, now: number, ttlMs = 5000): { order: string[]; pending: PendingMove | null } {
  if (!pending) return { order: server, pending: null };
  const same = server.length === pending.order.length && server.every((k, i) => k === pending.order[i]);
  const sameSet = server.length === pending.order.length && server.every((k) => pending.order.includes(k));
  if (same || !sameSet || !server.includes(pending.key) || now - pending.at > ttlMs) return { order: server, pending: null };
  return { order: pending.order, pending };
}
/** The `before` key for moving `key` one step up/down within `order` (null = to the bottom); undefined = no-op. */
export function neighbourBefore(order: string[], key: string, dir: 'up' | 'down'): string | null | undefined {
  const i = order.indexOf(key);
  if (i < 0) return undefined;
  if (dir === 'up') return i === 0 ? undefined : order[i - 1];
  if (i >= order.length - 1) return undefined;
  return i + 2 < order.length ? order[i + 2] : null;
}

/** Ordered list for display: server order with an optional local override; archived excluded. */
export function orderTickets(list: TicketSummary[], order: string[] | null): TicketSummary[] {
  if (!order) return list;
  const by = new Map(list.map((t) => [t.key, t]));
  const out: TicketSummary[] = [];
  for (const k of order) { const t = by.get(k); if (t) { out.push(t); by.delete(k); } }
  return [...out, ...by.values()];
}

/** Selection survives updates: keep the selected key if still present, else the nearest row by index. */
export function keepSelection(prev: string | null, prevIndex: number, list: { key: string }[]): string | null {
  if (!list.length) return null;
  if (prev && list.some((t) => t.key === prev)) return prev;
  if (prev === null) return null;
  return list[Math.max(0, Math.min(prevIndex, list.length - 1))].key;
}

export const startModes = [
  { value: 'container' as const, label: 'Start in container' },
  { value: 'host' as const, label: 'Start on host' },
  { value: 'single' as const, label: 'Single message' },
];
export type QueueOf = Queue;
