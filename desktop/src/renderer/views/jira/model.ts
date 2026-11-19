// Jira view model: list/mine query, row filter, status category tone, "queued" label. Pure; tested in model.test.ts.
import type { JiraRow } from '../../../shared/api';

export type JiraList = 'sprint' | 'backlog';
export const JIRA_LISTS: JiraList[] = ['sprint', 'backlog'];

/** Query for GET /jira (serve: list=sprint|backlog, mine=1). */
export function jiraQuery(list: JiraList, mine: boolean): Record<string, string> {
  return mine ? { list, mine: '1' } : { list };
}

/** Rows matching a case-insensitive filter on key, summary, status, assignee or type. */
export function filterRows(rows: JiraRow[], q: string): JiraRow[] {
  const s = q.trim().toLowerCase();
  if (!s) return rows;
  return rows.filter((r) => [r.key, r.summary, r.status, r.assignee, r.type].some((v) => String(v ?? '').toLowerCase().includes(s)));
}

/** Pill tone for a Jira status category (serve's `cat`: new / indeterminate / done) or, without it, the status name. */
export function statusTone(r: Pick<JiraRow, 'status'> & { cat?: unknown }): 'ok' | 'info' | 'default' {
  const c = String(r.cat ?? '').toLowerCase();
  if (c === 'done') return 'ok';
  if (c === 'indeterminate') return 'info';
  if (c) return 'default';
  const s = String(r.status ?? '').toLowerCase();
  if (/\b(done|closed|resolved)\b/.test(s)) return 'ok';
  if (/progress|review/.test(s)) return 'info';
  return 'default';
}

/** Keys not yet in the queue, in order, deduplicated: what "Add to queue" would add. */
export function addableKeys(rows: JiraRow[], picked: Set<string>): string[] {
  const out: string[] = [];
  for (const r of rows) if (picked.has(r.key) && !r.queued && !out.includes(r.key)) out.push(r.key);
  return out;
}
