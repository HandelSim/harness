// Home model (PLAN 3.3). Pure; unit-tested. Server values are read defensively: usage is read_ledger()'s dict
// (models and remaining arrive as JSON arrays of tuples), activity is vault_log() rows.
import type { Queue, TicketStatus } from '../../../shared/api';

const num = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) ? v : 0);

export interface UsageModel { name: string; replies: number; prompt: number; completion: number; total: number; pct: number }
export interface UsageView {
  tiles: { label: string; value: number }[];
  hour: { replies: number; total: number };
  reasoning: number; cached: number;
  models: UsageModel[];
  remaining: string;
}

/** read_ledger()'s dict as tiles + a per-model table (top 50, share of the biggest); null when there is none. */
export function usageView(u: unknown, cap = 50): UsageView | null {
  if (!u || typeof u !== 'object') return null;
  const o = u as Record<string, unknown>;
  const d = (o.day && typeof o.day === 'object' ? o.day : {}) as Record<string, unknown>;
  const h = (o.hour && typeof o.hour === 'object' ? o.hour : {}) as Record<string, unknown>;
  const models: UsageModel[] = [];
  for (const row of Array.isArray(o.models) ? o.models.slice(0, cap) : []) {
    if (!Array.isArray(row) || typeof row[0] !== 'string') continue;
    const v = Array.isArray(row[1]) ? row[1] : [];
    const prompt = num(v[1]), completion = num(v[2]);
    models.push({ name: row[0], replies: num(v[0]), prompt, completion, total: prompt + completion, pct: 0 });
  }
  const top = Math.max(1, ...models.map((m) => m.total));
  for (const m of models) m.pct = Math.round((100 * m.total) / top);
  const remaining = (Array.isArray(o.remaining) ? o.remaining : [])
    .filter((x): x is [unknown, unknown] => Array.isArray(x) && x.length >= 2)
    .map(([k, v]) => `${String(k)} ${String(v)}`).join(', ');
  const prompt = num(d.prompt), completion = num(d.completion);
  return {
    tiles: [
      { label: 'Replies', value: num(d.replies) }, { label: 'Prompt tokens', value: prompt },
      { label: 'Completion tokens', value: completion }, { label: 'Total tokens', value: num(d.total) || prompt + completion },
    ],
    hour: { replies: num(h.replies), total: num(h.total) }, reasoning: num(d.reasoning), cached: num(d.cached),
    models, remaining,
  };
}

export interface QueueSummary {
  total: number; running: number; waiting: number; queued: number; stuck: number; done: number;
  /** One line about the runner: "running DEMO-3 implement", "starting", "halting", "idle". */
  runner: string; runnerOn: boolean;
  /** Tickets that need the owner (answers or a plan review), newest first, at most 5. */
  attention: { key: string; title: string; status: TicketStatus }[];
}

export function queueSummary(q: Queue | undefined | null): QueueSummary | null {
  if (!q || !Array.isArray(q.tickets)) return null;
  const s: QueueSummary = { total: q.tickets.length, running: 0, waiting: 0, queued: 0, stuck: 0, done: 0, runner: 'idle', runnerOn: false, attention: [] };
  for (const t of q.tickets) {
    switch (t.status) {
      case 'running': s.running++; break;
      case 'needs-answers': case 'plan-review': s.waiting++; s.attention.push({ key: t.key, title: t.title, status: t.status }); break;
      case 'queued': s.queued++; break;
      case 'done': s.done++; break;
      default: s.stuck++;
    }
  }
  s.attention = s.attention.slice(0, 5);
  const r = q.runner;
  if (r) {
    if (r.halting) { s.runner = 'halting'; s.runnerOn = true; }
    else if (r.starting) { s.runner = 'starting'; s.runnerOn = true; }
    else if (r.running) {
      s.runnerOn = true;
      s.runner = ['running', r.ticket, r.stage, r.step].filter((x) => typeof x === 'string' && x).join(' ');
    }
  }
  return s;
}

export interface ActivityRow { sha: string; author: string; when: string; subject: string }
/** vault_log() rows, checked; at most cap. */
export function activityRows(a: unknown, cap = 30): ActivityRow[] {
  const out: ActivityRow[] = [];
  for (const r of Array.isArray(a) ? a : []) {
    if (!r || typeof r !== 'object') continue;
    const o = r as Record<string, unknown>;
    const subject = typeof o.subject === 'string' ? o.subject : '';
    if (!subject) continue;
    out.push({ sha: String(o.sha ?? ''), author: String(o.author ?? ''), when: String(o.when ?? ''), subject });
    if (out.length >= cap) break;
  }
  return out;
}

/** Pages filtered by title or path (the web home's ?q=), at most cap. */
export function filterPages<T extends { rel: string; title: string }>(pages: T[] | undefined, q: string, cap = 50): { shown: T[]; more: number; total: number } {
  const all = Array.isArray(pages) ? pages : [];
  const t = q.trim().toLowerCase();
  const hit = t ? all.filter((p) => `${p.title} ${p.rel}`.toLowerCase().includes(t)) : all;
  return { shown: hit.slice(0, cap), more: Math.max(0, hit.length - cap), total: all.length };
}

export function greeting(hour: number): string {
  if (hour < 5) return 'Working late';
  if (hour < 12) return 'Good morning';
  if (hour < 18) return 'Good afternoon';
  return 'Good evening';
}

/** Web path of a published page, built from its rel path (serve's `url` is "/pub/" + rel unencoded, so a `#`, `?` or `%`
 *  in a name would open the wrong page). */
export const pubPath = (rel: string): string => '/pub/' + rel.split('/').map(encodeURIComponent).join('/');
