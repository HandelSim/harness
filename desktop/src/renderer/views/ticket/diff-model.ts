// Changes tab model (PLAN W5 step 13): unified diff -> files/rows via diff2html's parser, render caps, review
// comments (the server's fix item shape), show-more-lines merge. Pure; unit-tested in diff-model.test.ts.
import { parse } from 'diff2html/lib-esm/diff-parser';
import type { DiffFile } from 'diff2html/lib-esm/types';
import { FIX_MAX_CHARS, FIX_MAX_COMMENTS } from './model';

/** Files whose lines render up front; the rest start collapsed (one click each). */
export const OPEN_FILES = 8;
/** Lines one file renders before "Show all N lines"; a file's lines past this are never in the DOM until asked. */
export const FILE_LINE_CAP = 400;
/** A diff text over this many characters is parsed only up to here (the rest is listed as cut). */
export const DIFF_CHAR_CAP = 6 * 1024 * 1024;
/** Server cap for one /lines request (diff_view.LINES_CAP). */
export const LINES_CAP = 500;

export type RowKind = 'hunk' | 'ctx' | 'add' | 'del';
export interface Row { kind: RowKind; old?: number; new?: number; text: string }
export interface FileView {
  path: string; oldPath: string; added: number; deleted: number; binary: boolean;
  status: 'added' | 'deleted' | 'renamed' | 'modified'; rows: Row[];
}

const isDevNull = (p: string) => !p || p === '/dev/null';

/** Parse a unified diff into renderable files. Never throws: a parser failure gives []. */
export function parseDiff(text: string): { files: FileView[]; cut: boolean } {
  const cut = text.length > DIFF_CHAR_CAP;
  let raw: DiffFile[] = [];
  try { raw = parse(cut ? text.slice(0, text.lastIndexOf('\ndiff ', DIFF_CHAR_CAP) > 0 ? text.lastIndexOf('\ndiff ', DIFF_CHAR_CAP) : DIFF_CHAR_CAP) : text); } catch { raw = []; }
  return { files: raw.map(toView), cut };
}

function toView(f: DiffFile): FileView {
  const rows: Row[] = [];
  for (const b of f.blocks) {
    rows.push({ kind: 'hunk', text: b.header });
    for (const l of b.lines) {
      const text = l.content.length ? l.content.slice(1) : '';
      if (l.type === 'insert') rows.push({ kind: 'add', new: l.newNumber, text });
      else if (l.type === 'delete') rows.push({ kind: 'del', old: l.oldNumber, text });
      else rows.push({ kind: 'ctx', old: l.oldNumber, new: l.newNumber, text });
    }
  }
  const deleted = !!f.isDeleted || (isDevNull(f.newName) && !isDevNull(f.oldName));
  const added = !!f.isNew || (isDevNull(f.oldName) && !isDevNull(f.newName));
  const path = deleted ? f.oldName : f.newName;
  return {
    path, oldPath: f.oldName, added: f.addedLines, deleted: f.deletedLines, binary: !!f.isBinary,
    status: added ? 'added' : deleted ? 'deleted' : f.isRename || (f.oldName !== f.newName && !isDevNull(f.oldName)) ? 'renamed' : 'modified',
    rows,
  };
}

/** Totals for the header. */
export function totals(files: FileView[]): { files: number; added: number; deleted: number } {
  let added = 0; let deleted = 0;
  for (const f of files) { added += f.added; deleted += f.deleted; }
  return { files: files.length, added, deleted };
}

// ---------------------------------------------------------------------------------------------- comments
/** One review comment as kept in the draft (where it was made travels with it). */
export interface ReviewComment {
  text: string; path?: string; line?: number; side?: 'new' | 'old'; code?: string; repo?: string; view?: string;
}
/** The line a row comments on: the new side when it has one (added/context), the old side for a deleted line. */
export function rowAnchor(r: Row): { line: number; side: 'new' | 'old' } | null {
  if (r.kind === 'hunk') return null;
  if (r.kind === 'del') return r.old ? { line: r.old, side: 'old' } : null;
  return r.new ? { line: r.new, side: 'new' } : null;
}
export function anchorKey(c: Pick<ReviewComment, 'repo' | 'view' | 'path' | 'side' | 'line'>): string {
  return [c.repo || '.', c.view || 'work', c.path ?? '', c.side ?? '', c.line ?? ''].join('\n');
}
/** Views whose lines take comments (the server accepts work and uncommitted only). */
export function viewTakesComments(view: string): boolean { return view === 'work' || view === 'uncommitted'; }

/** POST /tickets/K/fix body items: the web page's shape ({path,line,side,code,text} + repo/view when not the default). */
export function fixItems(cs: ReviewComment[]): Record<string, unknown>[] {
  return cs.map((c) => {
    if (!c.path) return { text: c.text };
    const o: Record<string, unknown> = { path: c.path, line: c.line, side: c.side, code: (c.code ?? '').slice(0, 500), text: c.text };
    if (c.repo && c.repo !== '.') o.repo = c.repo;
    if (c.view === 'uncommitted') o.view = 'uncommitted';
    return o;
  });
}
/** '' when the draft can be sent, else why not (client twin of the server cap). */
export function reviewError(cs: ReviewComment[]): string {
  if (!cs.length) return 'Click a line of the diff to comment on it, or write a comment on the whole change.';
  if (cs.length > FIX_MAX_COMMENTS) return `At most ${FIX_MAX_COMMENTS} comments go to one fix (${cs.length} now).`;
  const long = cs.findIndex((c) => c.text.length > FIX_MAX_CHARS);
  if (long >= 0) return `Comment ${long + 1} is over ${FIX_MAX_CHARS} characters.`;
  if (cs.some((c) => !c.text.trim())) return 'A comment is empty.';
  return '';
}
/** Add or replace (same anchor) a comment; refuses past the cap by returning the same array. */
export function putComment(cs: ReviewComment[], c: ReviewComment): ReviewComment[] {
  const text = c.text.slice(0, FIX_MAX_CHARS);
  if (c.path) {
    const k = anchorKey(c);
    const i = cs.findIndex((x) => x.path && anchorKey(x) === k);
    if (i >= 0) return text.trim() ? cs.map((x, j) => (j === i ? { ...c, text } : x)) : cs.filter((_, j) => j !== i);
  }
  if (!text.trim() || cs.length >= FIX_MAX_COMMENTS) return cs;
  return [...cs, { ...c, text }];
}
/** Where a comment is, for the tray. */
export function commentWhere(c: ReviewComment): string {
  if (!c.path) return 'the whole change';
  const repo = c.repo && c.repo !== '.' ? `${c.repo}: ` : '';
  return `${repo}${c.path}:${c.line}${c.side === 'old' ? ' (old)' : ''}${c.view === 'uncommitted' ? ', uncommitted' : ''}`;
}

// ---------------------------------------------------------------------------------------------- show more lines
/** The gap of new-side lines above hunk row index i (1-based, inclusive), or null when there is none. */
export function gapAbove(rows: Row[], i: number): { from: number; to: number } | null {
  if (rows[i]?.kind !== 'hunk') return null;
  let prevNew = 0;
  for (let j = i - 1; j >= 0; j--) { const n = rows[j].new; if (n) { prevNew = n; break; } }
  let nextNew = 0;
  for (let j = i + 1; j < rows.length; j++) { const r = rows[j]; if (r.kind === 'hunk') break; if (r.new) { nextNew = r.new; break; } }
  if (!nextNew) {
    const m = /\+(\d+)/.exec(rows[i].text);
    nextNew = m ? Number(m[1]) : 0;
  }
  if (nextNew - 1 <= prevNew) return null;
  const to = nextNew - 1;
  return { from: Math.max(prevNew + 1, to - LINES_CAP + 1), to };
}
/** Insert fetched new-side lines (from..) as context rows at the top of hunk i (its header moves up to take them, as
 * the web page does); old numbers keep the hunk's offset, so a gap left above stays reachable from the same header. */
export function insertLines(rows: Row[], i: number, from: number, lines: string[]): Row[] {
  if (rows[i]?.kind !== 'hunk' || !lines.length) return rows;
  let delta = 0;   // old - new at the hunk start
  const m = /-(\d+)(?:,\d+)? \+(\d+)/.exec(rows[i].text);
  if (m) delta = Number(m[1]) - Number(m[2]);
  const add: Row[] = lines.map((text, k) => ({ kind: 'ctx' as const, new: from + k, old: from + k + delta, text }));
  return [...rows.slice(0, i + 1), ...add, ...rows.slice(i + 1)];
}
