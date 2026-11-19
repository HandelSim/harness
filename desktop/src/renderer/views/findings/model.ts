// Findings model: the board (serve/findings_view.py) and the findings agent card (serve/findings_agent_view.py),
// read from GET /findings and /findings/agent. Pure; unit-tested. Every field is read defensively.
import type { Finding, FindingsAgent } from '../../../shared/api';

export const OPEN_CAP = 300;       // findings_view.OPEN_CAP: open findings shown
export const ARCHIVED_CAP = 200;   // findings_view.ARCHIVED_CAP
export const STALE_BEAT = 300;     // findings_agent_view.STALE_BEAT (s)
export const NOTES_MAX = 4000;     // POST /findings/agent/notes cap
export const LENSES = ['bug', 'verify', 'cleanup', 'trace', 'ui'];

const str = (v: unknown, cap = 4000): string => (typeof v === 'string' ? v.slice(0, cap) : typeof v === 'number' ? String(v) : '');
const int = (v: unknown): number | null => (typeof v === 'number' && Number.isInteger(v) ? v : null);
const obj = (v: unknown): Record<string, unknown> => (v && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : {});

export interface RollingItem { id: string; path: string; line: string; note: string; evidence: string }
export interface FindingView {
  id: string; title: string; archived: boolean;
  src: string;              // 'agent' or the ticket key that noted it
  key: string;              // ticket key ('' for the agent's)
  cat: string; sev: string; path: string; line: string; ms: number | null; project: string;
  body: string; evidence: string; md: string;
  bug: { ui: string; steps: string[]; expected: string; actual: string; verdict: string; trace: string[] } | null;
  rolling: RollingItem[];
  hay: string;              // lower-case text the filter searches
}

export function findingView(f: Finding): FindingView {
  const o = f as unknown as Record<string, unknown>;
  const id = str(o.id, 200), title = str(o.title, 400) || '(untitled)';
  const steps = Array.isArray(o.steps) ? o.steps.map((s) => str(s, 1000)).filter(Boolean) : [];
  const trace = Array.isArray(o.trace) ? o.trace.map((t) => {
    const x = obj(t); const p = str(x.path, 400);
    return [p && `${p}${int(x.line) ? `:${x.line}` : ''}`, str(x.code, 400)].filter(Boolean).join('  ');
  }).filter(Boolean) : [];
  const bugish = steps.length || trace.length || o.expected || o.actual || o.verdict || o.ui;
  const rolling = str(o.kind) === 'rolling' && Array.isArray(o.items)
    ? o.items.map((i) => obj(i)).filter((i) => str(i.id)).slice(0, 500).map((i) => ({
      id: str(i.id, 200), path: str(i.path, 400), line: int(i.line) ? String(i.line) : '', note: str(i.note, 2000), evidence: str(i.evidence, 2000) }))
    : [];
  const src = str(o.src, 100) || (str(o.key) ? str(o.key) : 'agent');
  const v: FindingView = {
    id, title, archived: o.archived === true, src, key: str(o.key, 100), cat: str(o.cat, 40), sev: str(o.sev, 20),
    path: str(o.path, 400), line: int(o.line) ? String(o.line) : '', ms: typeof o.ms === 'number' ? o.ms : null,
    project: str(o.project, 400), body: str(o.body) || str(o.text), evidence: str(o.evidence), md: str(o.md, 8000),
    bug: bugish ? { ui: str(o.ui, 1000), steps, expected: str(o.expected, 2000), actual: str(o.actual, 2000), verdict: str(o.verdict, 2000), trace } : null,
    rolling, hay: '',
  };
  v.hay = [v.title, v.body, v.path, v.src, v.cat, v.evidence].join(' ').toLowerCase();
  return v;
}

/** The text Copy puts on the clipboard: the finding's md line when serve sent one, else a markdown line of its own. */
export function copyText(v: FindingView): string {
  if (v.md) return v.md;
  const where = v.path ? ` (${v.path}${v.line ? `:${v.line}` : ''})` : '';
  return `- ${v.sev ? `[${v.sev}] ` : ''}${v.title}${where}${v.body ? `: ${v.body.split('\n')[0]}` : ''}`;
}

export interface Filter { q: string; src: string; cat: string }
export function filterFindings(rows: FindingView[], f: Filter): FindingView[] {
  const q = f.q.trim().toLowerCase();
  if (!q && !f.src && !f.cat) return rows;
  return rows.filter((r) => (!f.src || r.src === f.src) && (!f.cat || r.cat === f.cat) && (!q || r.hay.includes(q)));
}

/** The source and category choices of the filter, each sorted ('agent' first among sources). */
export function facets(rows: FindingView[]): { sources: string[]; cats: string[] } {
  const s = new Set<string>(), c = new Set<string>();
  for (const r of rows) { if (r.src) s.add(r.src); if (r.cat) c.add(r.cat); }
  const sources = [...s].sort((a, b) => (a === 'agent' ? -1 : b === 'agent' ? 1 : a.localeCompare(b)));
  return { sources, cats: [...c].sort() };
}

/** Split serve's items into the open board (in its order) and the archived list, each capped like the web page. */
export function board(items: Finding[] | undefined): { open: FindingView[]; archived: FindingView[]; openTotal: number; archivedTotal: number } {
  const open: FindingView[] = [], archived: FindingView[] = [];
  for (const f of Array.isArray(items) ? items : []) {
    if (!f || typeof f !== 'object') continue;
    const v = findingView(f);
    if (!v.id) continue;
    (v.archived ? archived : open).push(v);
  }
  return { open: open.slice(0, OPEN_CAP), archived: archived.slice(0, ARCHIVED_CAP), openTotal: open.length, archivedTotal: archived.length };
}

/** POST /findings/move's `before` for moving id one place up (-1) or down (+1) on the open board: the id it goes
 *  before, '' for the bottom; null when it cannot move. */
export function moveTarget(ids: string[], id: string, dir: -1 | 1): string | null {
  const i = ids.indexOf(id);
  if (i < 0) return null;
  if (dir < 0) return i === 0 ? null : ids[i - 1];
  if (i >= ids.length - 1) return null;
  return i + 2 < ids.length ? ids[i + 2] : '';
}

export type Tone = 'ok' | 'warn' | 'err' | 'info' | 'accent' | 'default';
const STATES: Record<string, [Tone, string]> = {
  exploring: ['info', 'exploring'], scanning: ['info', 'exploring'], waiting: ['info', 'waiting between passes'],
  idle: ['warn', 'idle'], backoff: ['warn', 'backing off'], paused: ['warn', 'paused'], stopping: ['default', 'stopping'], stopped: ['default', 'stopped'],
};

export function agoS(secs: number | null): string {
  if (secs === null) return 'never';
  if (secs < 90) return `${Math.max(0, Math.round(secs))} s ago`;
  if (secs < 5400) return `${Math.round(secs / 60)} min ago`;
  if (secs < 172800) return `${Math.floor(secs / 3600)} h ago`;
  return `${Math.floor(secs / 86400)} days ago`;
}

export interface AgentView {
  live: boolean; label: string; tone: Tone; started: boolean;
  info: [string, string][];
  counts: { lens: string; calls: number; kept: number; thrown: number; why: string; failed: number; notes: number }[];
  passes: { outcome: string; tone: Tone; label: string; session: string; found: number; started: string; covered: string }[];
  hasPastSession: boolean;
}

const OUTCOME_TONE: Record<string, Tone> = { found: 'ok', waiting: 'warn', none: 'default', running: 'info', failed: 'err', stopped: 'default' };

/** The agent card as data (findings_agent_view.agent_card): state label and tone, info rows, per-question counts, passes. */
export function agentView(fa: FindingsAgent | undefined | null, nowS: number): AgentView | null {
  if (!fa || typeof fa !== 'object') return null;
  const info = obj(fa.status);
  const beat = typeof (fa as unknown as { heartbeat?: unknown }).heartbeat === 'number' ? (fa as unknown as { heartbeat: number }).heartbeat : null;
  const age = beat === null ? null : Math.round(nowS - beat);
  const state = str(info.state, 20);
  const started = Object.keys(info).length > 0;
  const live = fa.running === true;
  let tone: Tone, label: string;
  if (!started) [tone, label] = ['default', 'never started'];
  else if (live) [tone, label] = STATES[state] ?? ['default', state || 'unknown'];
  else if (state === '' || state === 'stopped') [tone, label] = ['default', 'stopped'];
  else [tone, label] = ['err', `not answering (last: ${state}, heartbeat ${agoS(age)})`];
  const rows: [string, string][] = [];
  const s = (k: string, cap = 400) => str(info[k], cap).replace(/\s+/g, ' ').trim();
  if (s('model')) rows.push(['model', s('model')]);
  if (started) rows.push(['heartbeat', agoS(age) + (age !== null && age > STALE_BEAT && state !== '' && state !== 'stopped' ? ' (stale)' : '')]);
  if (live && s('unit')) rows.push(['on', s('unit')]);
  if (live && s('reason')) rows.push(['why', s('reason')]);
  if (live && s('next_at')) rows.push(['next', s('next_at')]);
  if (Array.isArray(info.repos) && info.repos.length) rows.push(['repos', info.repos.filter((r) => typeof r === 'string').slice(0, 10).join(', ')]);
  if (live && s('session', 80).startsWith('ses_')) rows.push(['session', s('session', 80)]);
  for (const [k, name] of [['open', 'open findings'], ['pending', 'reports waiting for the checker'], ['todo', 'changed passes first'], ['units', 'passes in the rotation']] as const) {
    const v = int(info[k]); if (v !== null) rows.push([name, String(v)]);
  }
  if (s('last_error', 600)) rows.push(['last error', s('last_error', 600)]);
  const c = obj(info.counts);
  const counts = Object.keys(c).sort((a, b) => {
    const ia = LENSES.indexOf(a), ib = LENSES.indexOf(b);
    return (ia < 0 ? 9 : ia) - (ib < 0 ? 9 : ib) || a.localeCompare(b);
  }).filter((k) => c[k] && typeof c[k] === 'object').map((lens) => {
    const v = obj(c[lens]); const n = (k: string) => int(v[k]) ?? 0;
    const why = Object.entries(obj(v.why)).filter(([, x]) => int(x) !== null)
      .sort((a, b) => (b[1] as number) - (a[1] as number) || a[0].localeCompare(b[0])).slice(0, 8).map(([k, x]) => `${k.slice(0, 40)} ${x}`).join(', ');
    return { lens: lens.slice(0, 20), calls: n('calls'), kept: n('accepted'), thrown: n('rejected'), why, failed: n('failed'), notes: n('notes') };
  });
  const passes = (Array.isArray(fa.passes) ? fa.passes : []).filter((p) => p && typeof p === 'object').slice(0, 20).map((p0) => {
    const p = obj(p0);
    const outcome = str(p.outcome, 20);
    const repo = str(p.repo, 300).replace(/[\\/]+$/, '').split(/[\\/]/).pop() || '';
    const sid = str(p.session, 80);
    return { outcome: outcome || '?', tone: OUTCOME_TONE[outcome] ?? 'default', label: `${repo}: ${str(p.area, 200)}/ (${str(p.lens, 20)})`,
      session: /^ses_[A-Za-z0-9_]+$/.test(sid) ? sid : '', found: int(p.found) ?? 0, started: str(p.started, 30), covered: str(p.covered, 300) };
  });
  return { live, label, tone, started, info: rows, counts, passes, hasPastSession: passes.slice(0, 5).some((p) => p.session) };
}

/** The Start form's folder choices: saved folders (ticked) then the last run's repos not saved (unticked), deduped. */
export function folderChoices(fa: FindingsAgent | undefined | null): { dir: string; on: boolean }[] {
  const out: { dir: string; on: boolean }[] = [];
  const seen = new Set<string>();
  const strs = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string' && !!x) : []);
  const saved = strs(fa?.folders), last = strs(fa?.last_repos);
  for (const d of saved) if (!seen.has(d)) { seen.add(d); out.push({ dir: d, on: true }); }
  for (const d of last) if (!seen.has(d)) { seen.add(d); out.push({ dir: d, on: !saved.length }); }
  return out.slice(0, 40);
}

/** "Other folders, one per line" as a list: trimmed, no blanks, no duplicates of the ticked ones. */
export function extraFolders(text: string, ticked: string[]): string[] {
  const t = new Set(ticked);
  const out: string[] = [];
  for (const l of text.split(/\r?\n/)) { const s = l.trim(); if (s && !t.has(s) && !out.includes(s)) out.push(s); }
  return out.slice(0, 20);
}

export interface NoteRef { slug: string; name: string; title: string; repo: string; area: string; updated: string; summary: string; lens: string }
export interface NoteGroup { repo: string; notes: NoteRef[] }
const SLUG_RE = /^[A-Za-z0-9_][A-Za-z0-9._-]{0,200}$/;

/** The knowledge index grouped by repo (basename), each sorted by area; entries with unsafe slugs/names dropped. */
export function groupKnowledge(k: unknown, q = ''): NoteGroup[] {
  const by = new Map<string, NoteRef[]>();
  const t = q.trim().toLowerCase();
  for (const e of Array.isArray(k) ? k : []) {
    const o = obj(e);
    const slug = str(o.slug, 201), name = str(o.name, 201);
    if (!SLUG_RE.test(slug) || !SLUG_RE.test(name)) continue;
    const repoPath = str(o.repo, 400);
    const repo = repoPath.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || slug;
    const n: NoteRef = { slug, name, title: str(o.title, 300) || str(o.area, 300) || name, repo, area: str(o.area, 300), updated: str(o.updated, 40), summary: str(o.summary, 600), lens: str(o.lens, 20) };
    if (t && !`${n.title} ${n.area} ${n.repo} ${n.summary}`.toLowerCase().includes(t)) continue;
    const l = by.get(repo) ?? [];
    l.push(n); by.set(repo, l);
  }
  return [...by.entries()].sort((a, b) => a[0].localeCompare(b[0]))
    .map(([repo, notes]) => ({ repo, notes: notes.sort((a, b) => (a.area || a.title).localeCompare(b.area || b.title)) }));
}

export const notePath = (n: { slug: string; name: string }): string => `/findings/notes/${encodeURIComponent(n.slug)}/${encodeURIComponent(n.name)}`;
