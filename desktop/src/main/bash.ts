// Which bash runs `harness`. On macOS/Linux it is `bash` from PATH. On Windows the app is usually started
// from a shortcut, where `bash` on PATH is WSL's System32\bash.exe (wrong distro, wrong paths) or missing
// (Git for Windows puts only Git\cmd on PATH), so look for Git Bash's launcher, Git\bin\bash.exe: it sets
// up the MSYS PATH (/usr/bin, /mingw64/bin) the harness script needs.
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { win32 } from 'node:path';

export interface BashIn { platform: NodeJS.Platform; env: Record<string, string | undefined>; exists(p: string): boolean }

export function findBash(i: BashIn): string {
  const override = i.env.HARNESS_DESKTOP_BASH;
  if (override && i.exists(override)) return override;
  if (i.platform !== 'win32') return 'bash';
  const cands: string[] = [];
  for (const k of ['ProgramW6432', 'ProgramFiles', 'ProgramFiles(x86)']) {
    const d = i.env[k];
    if (d) cands.push(win32.join(d, 'Git', 'bin', 'bash.exe'));
  }
  if (i.env.LOCALAPPDATA) cands.push(win32.join(i.env.LOCALAPPDATA, 'Programs', 'Git', 'bin', 'bash.exe'));
  if (i.env.USERPROFILE) cands.push(win32.join(i.env.USERPROFILE, 'scoop', 'apps', 'git', 'current', 'bin', 'bash.exe'));
  // A Git install elsewhere: its cmd\ (or bin\) folder is on PATH.
  for (const d of (i.env.Path || i.env.PATH || '').split(';').filter(Boolean)) {
    if (i.exists(win32.join(d, 'git.exe'))) cands.push(win32.join(d, '..', 'bin', 'bash.exe'));
    if (/\\(system32|windowsapps)\\?$/i.test(d)) continue;
    cands.push(win32.join(d, 'bash.exe'));
  }
  for (const c of cands) if (i.exists(c)) return win32.normalize(c);
  return 'bash';
}

let cached: string | null = null;
/** The bash for this process, resolved once. */
export function bashPath(): string {
  if (cached === null) cached = findBash({ platform: process.platform, env: process.env, exists: existsSync });
  return cached;
}

/** Windows has no process groups: end a child and everything it started with taskkill /T. Returns false
 *  elsewhere so callers signal the process group as before. */
export function killTree(pid: number, platform: NodeJS.Platform = process.platform): boolean {
  if (platform !== 'win32') return false;
  try {
    spawn('taskkill', ['/pid', String(pid), '/T', '/F'], { stdio: 'ignore', windowsHide: true }).on('error', () => { /* gone */ });
    return true;
  } catch { return false; }
}
