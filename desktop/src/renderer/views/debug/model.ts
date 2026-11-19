// Debug view model: filter the /debug list (kind + text), verdict tone, report path. Pure; tested in model.test.ts.
import type { DebugReport } from '../../../shared/api';

export type DebugKind = 'all' | 'runs' | 'reports';
// serve sends change as a boolean (shared/api.ts types it as a string), report as a boolean
export type DebugRow = Omit<DebugReport, 'change'> & { report?: boolean; change?: unknown };

/** Stable id of a report in the list (also what the panel remembers as the selection). */
export const debugId = (r: Pick<DebugReport, 'kind' | 'name'>): string => `${r.kind}/${r.name}`;
/** GET path of one report's text (serve: /debug/<runs|reports>/<name>). */
export const reportPath = (r: Pick<DebugReport, 'kind' | 'name'>): string =>
  `/debug/${encodeURIComponent(r.kind)}/${encodeURIComponent(r.name)}`;

/** Rows of a kind matching a case-insensitive filter on name, ticket key, title or verdict; server order kept. */
export function filterDebug(rows: DebugRow[], kind: DebugKind, q: string): DebugRow[] {
  const s = q.trim().toLowerCase();
  return rows.filter((r) => (kind === 'all' || r.kind === kind)
    && (!s || [r.name, r.key, r.title, r.verdict].some((v) => String(v ?? '').toLowerCase().includes(s))));
}

export function verdictTone(v: string): 'info' | 'ok' | 'warn' | 'err' | 'default' {
  const s = (v || '').toLowerCase();
  if (!s) return 'default';
  if (s === 'running') return 'info';
  if (/(no[- ]?bug|not a bug|works|ok|fine|cannot reproduce|no change)/.test(s)) return 'ok';
  if (/(unclear|unknown|inconclusive|partial)/.test(s)) return 'warn';
  if (/(bug|broken|fail|regression|defect)/.test(s)) return 'err';
  return 'default';
}

/** The row to select after the list changes: the remembered one when still there, else the first. */
export function pickSelection(rows: DebugRow[], sel: string | undefined): string {
  if (sel && rows.some((r) => debugId(r) === sel)) return sel;
  return rows[0] ? debugId(rows[0]) : '';
}
