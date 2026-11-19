// Locate the harness install root (1.2 step 1). Filesystem access injectable for tests.
import { accessSync, constants, existsSync, realpathSync, statSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { delimiter, dirname, join, resolve } from 'node:path';

export interface Fs { isExec(p: string): boolean; exists(p: string): boolean; realpath(p: string): string }
export const realFs: Fs = {
  isExec(p) { try { accessSync(p, constants.X_OK); return statSync(p).isFile(); } catch { return false; } },
  exists: (p) => existsSync(p),
  realpath(p) { try { return realpathSync(p); } catch { return p; } },
};

export function validRoot(root: string, fs: Fs = realFs): boolean {
  return fs.isExec(join(root, 'harness')) && fs.exists(join(root, 'serve', 'pub.py'));
}

export interface LocateIn { envRoot: string | null; settingsRoot: string | null; appPath: string; packaged: boolean;
  pathEnv: string; home: string }

/** First hit wins: env, settings, the repo containing desktop/ (unpackaged), `harness` on PATH, ~/harness. */
export function locateRoot(i: LocateIn, fs: Fs = realFs): { root: string | null; tried: string[] } {
  const tried: string[] = [];
  const cands: string[] = [];
  if (i.envRoot) cands.push(i.envRoot);
  if (i.settingsRoot) cands.push(i.settingsRoot);
  if (!i.packaged) cands.push(resolve(i.appPath, '..'));
  for (const d of i.pathEnv.split(delimiter).filter(Boolean)) {
    const h = join(d, 'harness');
    if (fs.isExec(h)) cands.push(dirname(fs.realpath(h)));
  }
  cands.push(join(i.home, 'harness'));
  for (const c of cands) {
    const r = resolve(c);
    tried.push(r);
    if (validRoot(r, fs)) return { root: fs.realpath(r), tried };
  }
  return { root: null, tried };
}

/** serve's state_id: sha256(normcase(realpath(state_dir)))[:16] (pub.py state_id). */
export function stateId(root: string, fs: Fs = realFs, platform: NodeJS.Platform = process.platform): string {
  let p = fs.realpath(join(root, 'state'));
  if (platform === 'win32') p = p.replace(/\//g, '\\').toLowerCase(); // ntpath.normcase
  return createHash('sha256').update(p, 'utf8').digest('hex').slice(0, 16);
}
