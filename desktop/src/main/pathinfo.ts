// shell:path-info (M2, F1, F2): async folder + git probe. Nothing here blocks the main thread: fs.promises and
// async execFile, each raced against a deadline so a hung mount (NFS, sshfs, autofs) answers "unknown" instead.
import { execFile } from 'node:child_process';
import { readdir, realpath, stat } from 'node:fs/promises';
import { homedir } from 'node:os';
import { relative, resolve, isAbsolute } from 'node:path';
import type { GitProbe, PathInfo } from '../shared/ipc';

const FS_MS = 1500;
const GIT_MS = 1500;

function race<T>(p: Promise<T>, ms: number, fallback: T): Promise<T> {
  let t: NodeJS.Timeout;
  return Promise.race([p.catch(() => fallback), new Promise<T>((r) => { t = setTimeout(() => r(fallback), ms); })]).finally(() => clearTimeout(t));
}

function git(cwd: string, args: string[]): Promise<{ code: number; out: string; err: string }> {
  return new Promise((res) => {
    execFile('git', ['-C', cwd, ...args], { timeout: GIT_MS, killSignal: 'SIGKILL', env: { ...process.env, GIT_TERMINAL_PROMPT: '0', LC_ALL: 'C' } },
      (e, out, err) => res({ code: e ? (typeof (e as { code?: unknown }).code === 'number' ? (e as { code: number }).code : 1) : 0, out: String(out), err: String(err) }));
  });
}

/** One git probe: repo root, branch (or detached), whether HEAD has a commit, whether `dir` is below the root. */
export async function gitProbe(dir: string): Promise<GitProbe> {
  const none: GitProbe = { isRepo: false, root: '', branch: '', detached: false, hasCommits: false, inSubfolder: false };
  const top = await git(dir, ['rev-parse', '--show-toplevel']);
  if (top.code !== 0) {
    const e = top.err.trim();
    // "not a git repository" is the ordinary answer; anything else (dubious ownership, git missing) is worth showing
    return /not a git repository/i.test(e) || !e ? none : { ...none, error: e.split('\n').find((l) => l.trim()) ?? e };
  }
  const root = top.out.trim();
  const [sym, head] = await Promise.all([git(dir, ['symbolic-ref', '-q', '--short', 'HEAD']), git(dir, ['rev-parse', '-q', '--verify', 'HEAD^{commit}'])]);
  const branch = sym.code === 0 ? sym.out.trim() : '';
  const realRoot = await realpath(root).catch(() => root);
  const realDir = await realpath(dir).catch(() => dir);
  const rel = relative(realRoot, realDir);
  return { isRepo: true, root: realRoot, branch, detached: sym.code !== 0, hasCommits: head.code === 0,
    inSubfolder: rel !== '' && !rel.startsWith('..') && !isAbsolute(rel) };
}

export async function pathInfo(path: string, list: boolean, home = homedir()): Promise<PathInfo> {
  const abs = resolve(path.replace(/^~(?=$|\/)/, home));
  const gone: PathInfo = { exists: false, dir: false, git: null };
  const st = await race(stat(abs), FS_MS, null);
  if (!st) return gone;
  const real = await race(realpath(abs), FS_MS, abs);
  const info: PathInfo = { exists: true, dir: st.isDirectory(), git: null, real };
  if (!info.dir) return info;
  const [repo, entries] = await Promise.all([
    gitProbe(real),
    list ? race(readdir(real, { withFileTypes: true }).then((ds) => ds.filter((d) => d.isDirectory() && !d.name.startsWith('.')).map((d) => d.name).sort().slice(0, 500)), FS_MS, [] as string[]) : Promise.resolve(undefined),
  ]);
  info.repo = repo;
  if (repo.isRepo) info.git = { branch: repo.branch };
  if (entries) info.entries = entries;
  return info;
}
