// New session dialog model (W2/W3): form -> PtySpec, validation, recents. Pure; unit-tested.
import type { PtySpec, PathInfo } from '../../../shared/ipc';

export type Kind = PtySpec['kind'];
export type OpenIn = 'here' | 'right' | 'down';
export interface SessionForm {
  kind: Kind; cwd: string; yolo: boolean; normal: boolean; thoughts: boolean; net: boolean;
  ticket: string; resume: boolean; openIn: OpenIn;
}
export const KINDS: { value: Kind; label: string; cmd: string; blurb: string }[] = [
  { value: 'agent', label: 'Container agent', cmd: 'harness', blurb: 'opencode in the sandboxed container' },
  { value: 'host', label: 'Host agent', cmd: 'harness host', blurb: 'opencode on this machine, no firewall' },
  { value: 'shell', label: 'Container shell', cmd: 'harness shell', blurb: 'a shell inside the container' },
  { value: 'plain', label: 'Plain shell', cmd: '$SHELL', blurb: 'your login shell in the folder' },
];
export const isAgent = (k: Kind) => k === 'agent' || k === 'host';
const TICKET_RE = /^[A-Za-z0-9._-]{1,80}$/;

export function defaultForm(kind: Kind = 'agent', cwd = ''): SessionForm {
  return { kind, cwd, yolo: false, normal: false, thoughts: false, net: false, ticket: '', resume: false, openIn: 'here' };
}

export function isAbsolute(p: string): boolean { return p.startsWith('/') || /^[A-Za-z]:[\\/]/.test(p) || p.startsWith('\\\\'); }
/** Trim and drop a trailing separator (except for a root). */
export function normalizePath(p: string): string {
  let s = p.trim();
  while (s.length > 1 && /[\\/]$/.test(s) && !/^[A-Za-z]:[\\/]$/.test(s)) s = s.slice(0, -1);
  return s;
}
export function dirName(p: string): string {
  const s = normalizePath(p);
  const i = Math.max(s.lastIndexOf('/'), s.lastIndexOf('\\'));
  if (i < 0) return '';
  if (i === 0) return '/';
  return s.slice(0, i);
}

/** Folder message: null = fine (or still checking). */
export function folderError(cwd: string, info: PathInfo | null): string | null {
  const p = normalizePath(cwd);
  if (!p) return 'Choose a folder';
  if (!isAbsolute(p)) return 'Use an absolute path';
  if (!info) return null;
  if (!info.exists) return 'Folder does not exist';
  if (!info.dir) return 'Not a folder';
  return null;
}
export function ticketError(t: string): string | null {
  const s = t.trim();
  if (!s) return null;
  return TICKET_RE.test(s) ? null : 'Letters, digits, . _ - only';
}
export function needsHostConsent(kind: Kind, consent: boolean): boolean { return kind === 'host' && !consent; }

/** Form errors keyed by field; empty object = submittable. */
export function validate(f: SessionForm, info: PathInfo | null, hostConsent: boolean, consentTicked: boolean): Partial<Record<'cwd' | 'ticket' | 'consent', string>> {
  const e: Partial<Record<'cwd' | 'ticket' | 'consent', string>> = {};
  const fe = folderError(f.cwd, info);
  if (fe) e.cwd = fe;
  if (isAgent(f.kind)) { const te = ticketError(f.ticket); if (te) e.ticket = te; }
  if (needsHostConsent(f.kind, hostConsent) && !consentTicked) e.consent = 'Host mode needs your consent';
  return e;
}

/** Only the flags that apply to the kind are sent (PLAN 5.2: resume is honoured for host only). */
export function buildSpec(f: SessionForm): PtySpec {
  const spec: PtySpec = { kind: f.kind, cwd: normalizePath(f.cwd) };
  if (isAgent(f.kind)) {
    if (f.yolo) spec.yolo = true;
    if (f.normal) spec.normal = true;
    if (f.thoughts) spec.thoughts = true;
    if (f.net) spec.net = true;
    const t = f.ticket.trim();
    if (t && TICKET_RE.test(t)) spec.ticket = t;
  }
  if (f.kind === 'host' && f.resume) spec.resume = 'last';
  return spec;
}
export function formFromSpec(s: PtySpec, openIn: OpenIn = 'here'): SessionForm {
  return { kind: s.kind, cwd: s.cwd, yolo: !!s.yolo, normal: !!s.normal, thoughts: !!s.thoughts, net: !!s.net,
    ticket: s.ticket ?? '', resume: s.resume === 'last', openIn };
}

/** Most recent first, de-duplicated, capped. */
export function pushRecentProject(list: string[], path: string, cap = 10): string[] {
  const p = normalizePath(path);
  if (!p) return list.slice(0, cap);
  return [p, ...list.filter((x) => normalizePath(x) !== p)].slice(0, cap);
}

/** Completion candidates from the parent folder's entries matching the typed prefix. */
export function completions(typed: string, parentEntries: string[], recents: string[], max = 12): string[] {
  const t = typed.trim();
  const out: string[] = [];
  for (const r of recents) if (!t || r.toLowerCase().includes(t.toLowerCase())) out.push(r);
  const endsSep = /[\\/]$/.test(t);
  const dir = endsSep ? normalizePath(t) : dirName(t);
  const prefix = endsSep ? '' : t.slice(Math.max(t.lastIndexOf('/'), t.lastIndexOf('\\')) + 1);
  if (dir) {
    const sep = dir.includes('\\') && !dir.includes('/') ? '\\' : '/';
    for (const e of parentEntries) {
      if (e.startsWith('.') && !prefix.startsWith('.')) continue;
      if (!e.toLowerCase().startsWith(prefix.toLowerCase())) continue;
      out.push(dir === '/' ? `/${e}` : `${dir}${sep}${e}`);
    }
  }
  return [...new Set(out)].slice(0, max);
}

/** F1: which git case a folder is, as the New session and Add tickets dialogs say it. `root` is set when the folder is
 *  inside a repo but not its top folder (so a dialog can offer "Use the repo's top folder"). */
export interface GitCase { tone: 'ok' | 'info' | 'warn'; text: string; repo: boolean; branch: string; root?: string }
export function gitCase(info: PathInfo): GitCase {
  const r = info.repo;
  if (!r) return info.git ? { tone: 'ok', text: `git repo · ${info.git.branch || 'detached'}`, repo: true, branch: info.git.branch }
    : { tone: 'info', text: 'folder (not a git repo)', repo: false, branch: '' };
  if (!r.isRepo) return { tone: 'info', text: r.error ? `not a git repo (${r.error})` : 'folder (not a git repo)', repo: false, branch: '' };
  if (!r.hasCommits) return { tone: 'warn', text: `empty git repo: no commits yet${r.branch ? ` on ${r.branch}` : ''}`, repo: true, branch: r.branch };
  const where = r.detached ? 'detached HEAD (no branch checked out)' : r.branch;
  if (r.inSubfolder) return { tone: 'info', text: `inside the git repo at ${r.root} · ${where}`, repo: true, branch: r.branch, root: r.root };
  return { tone: r.detached ? 'warn' : 'ok', text: `git repo · ${where}`, repo: true, branch: r.branch };
}

/** W5 step 3: the existing branch named after the ticket (local first, then a remote-tracking one), or ''. */
export function ticketBranch(key: string, local: string[], remote: string[]): string {
  const k = key.trim();
  if (!k) return '';
  if (local.includes(k)) return k;
  return remote.find((r) => r.slice(r.indexOf('/') + 1) === k) || '';
}
