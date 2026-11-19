// The only place a PTY command line is built (5.2 allowlist). Pure; unit-tested.
import { statSync } from 'node:fs';
import type { PtySpec } from '../../shared/ipc';

export const TICKET_RE = /^[A-Z][A-Z0-9_]*-[0-9]+$/;          // the CLI's _ticket_key (<= 40 chars)
export const SESSION_RE = /^ses_[A-Za-z0-9]{1,64}$/;           // serve/runs.py SID_RE
export interface Argv { file: string; args: string[]; cwd: string; env: Record<string, string> }
export type SpecResult = { ok: true; argv: Argv } | { ok: false; error: { code: string; message: string } };

const bad = (message: string): SpecResult => ({ ok: false, error: { code: 'bad-spec', message } });

/** Validate a renderer PtySpec into a clean one (unknown keys dropped). */
export function cleanSpec(raw: unknown): PtySpec | null {
  if (typeof raw !== 'object' || raw === null) return null;
  const r = raw as Record<string, unknown>;
  if (!['agent', 'host', 'shell', 'plain'].includes(r.kind as string)) return null;
  if (typeof r.cwd !== 'string' || r.cwd.length > 4096 || r.cwd.includes('\0')) return null;
  const s: PtySpec = { kind: r.kind as PtySpec['kind'], cwd: r.cwd };
  for (const f of ['yolo', 'normal', 'thoughts', 'net'] as const) if (r[f] === true) s[f] = true;
  if (r.ticket !== undefined) { if (typeof r.ticket !== 'string') return null; s.ticket = r.ticket; }
  if (r.resume !== undefined) {
    if (r.resume === 'last') s.resume = 'last';
    else if (typeof r.resume === 'object' && r.resume && typeof (r.resume as { session?: unknown }).session === 'string') s.resume = { session: (r.resume as { session: string }).session };
    else return null;
  }
  return s;
}

export function buildArgv(spec: PtySpec, o: { root: string | null; shell: string | undefined; hostConsent: boolean; bash?: string;
  isDir?: (p: string) => boolean; platform?: NodeJS.Platform }): SpecResult {
  const isDir = o.isDir || ((p: string) => { try { return statSync(p).isDirectory(); } catch { return false; } });
  if (!spec.cwd.startsWith('/') && !/^[A-Za-z]:[\\/]/.test(spec.cwd)) return bad('cwd must be an absolute path');
  if (!isDir(spec.cwd)) return bad(`${spec.cwd} is not a folder`);
  const env: Record<string, string> = { TERM: 'xterm-256color', COLORTERM: 'truecolor', HARNESS_OPEN: '0' };
  if (spec.kind === 'plain') {
    // Windows: o.shell (SHELL) is not set outside Git Bash; the plain terminal is Git Bash's login shell.
    if ((o.platform || process.platform) === 'win32') return { ok: true, argv: { file: o.bash || 'bash', args: ['--login', '-i'], cwd: spec.cwd, env } };
    const sh = o.shell && o.shell.startsWith('/') ? o.shell : '/bin/bash';
    return { ok: true, argv: { file: sh, args: [], cwd: spec.cwd, env } };
  }
  if (!o.root) return bad('no harness install');
  const h = `${o.root}/harness`;
  const args: string[] = [h];
  if (spec.kind === 'host') args.push('host');
  if (spec.kind === 'shell') { args.push('shell'); return { ok: true, argv: { file: o.bash || 'bash', args, cwd: spec.cwd, env } }; }
  if (spec.yolo) args.push('--yolo');
  if (spec.normal) args.push('--normal');
  if (spec.thoughts) args.push('--thoughts');
  if (spec.net) { if (spec.kind !== 'agent') return bad('--net is only for container sessions'); args.push('--net'); }
  if (spec.ticket !== undefined) {
    const k = spec.ticket.toUpperCase();
    if (k.length > 40 || !TICKET_RE.test(k)) return bad('ticket must look like ABC-123');
    args.push('--ticket', k);
  }
  // --continue / --session are forwarded to opencode by both `harness` and `harness host` (their pass_args).
  if (spec.resume === 'last') args.push('--continue');
  else if (spec.resume) {
    if (!SESSION_RE.test(spec.resume.session)) return bad('bad session id');
    args.push('--session', spec.resume.session);
  }
  if (spec.kind === 'host' && o.hostConsent) env.HARNESS_HOST_CONFIRM = '1';
  return { ok: true, argv: { file: o.bash || 'bash', args, cwd: spec.cwd, env } };
}
