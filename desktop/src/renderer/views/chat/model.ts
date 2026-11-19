// Chat model: which sessions the Chat panel lists and which one it opens. Pure; unit-tested.
// serve's chat is opencode web (a browser UI); the native Chat lists sessions from GET /sessions and talks to the
// live ones through POST /sessions/<sid>/send (the transcript's composer).
import type { SessionRow } from '../../../shared/api';
import { sortSessions } from '../transcript/model';

export type Scope = 'live' | 'all';
export const LIST_CAP = 500;

/** The list: filtered by q, live (messageable) runs first, then working, then most recently updated; capped. */
export function chatRows(rows: SessionRow[] | undefined, q: string, scope: Scope, cap = LIST_CAP): SessionRow[] {
  const sorted = sortSessions(Array.isArray(rows) ? rows : [], q);
  const live: SessionRow[] = [], rest: SessionRow[] = [];
  for (const r of sorted) (r.live ? live : rest).push(r);
  return (scope === 'live' ? live : live.concat(rest)).slice(0, cap);
}

/** The session to show: the current one while it is still listed, else the first live one, else the first. */
export function pickSession(list: SessionRow[], current: string): string {
  if (current && list.some((r) => r.id === current)) return current;
  return (list.find((r) => r.live) ?? list[0])?.id ?? '';
}

/** One line under a row's title: ticket or kind, model, folder name. */
export function rowMeta(r: SessionRow): string {
  const dir = (r.directory || '').replace(/[\\/]+$/, '').split(/[\\/]/).pop() || '';
  return [r.ticket || (r.kind !== 'session' ? r.kind : ''), r.model, dir].filter(Boolean).join(' · ');
}

/** Step the selection by d (wrapping off): the id of the row d places from current, or current at the ends. */
export function step(list: SessionRow[], current: string, d: number): string {
  if (!list.length) return '';
  const i = list.findIndex((r) => r.id === current);
  const j = i < 0 ? (d > 0 ? 0 : list.length - 1) : Math.max(0, Math.min(list.length - 1, i + d));
  return list[j].id;
}
