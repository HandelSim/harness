import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, symlinkSync, realpathSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathInfo } from './pathinfo';

const g = (cwd: string, ...a: string[]) => execFileSync('git', ['-C', cwd, '-c', 'user.name=t', '-c', 'user.email=t@t', '-c', 'init.defaultBranch=main', ...a], { stdio: 'ignore' });

test('F1/F2: path-info git probe covers no-commits, detached, subfolder, symlink and plain folders', { timeout: 30000 }, async () => {
  const base = realpathSync(mkdtempSync(join(tmpdir(), 'w1a-pi-')));
  try {
    const plain = join(base, 'plain'); mkdirSync(plain);
    const fresh = join(base, 'fresh'); mkdirSync(fresh); g(fresh, 'init', '-q');
    const repo = join(base, 'repo'); mkdirSync(join(repo, 'sub', 'deep'), { recursive: true }); g(repo, 'init', '-q'); g(repo, 'commit', '-q', '--allow-empty', '-m', 'a');
    const det = join(base, 'det'); mkdirSync(det); g(det, 'init', '-q'); g(det, 'commit', '-q', '--allow-empty', '-m', 'a'); g(det, 'checkout', '-q', '--detach');
    const link = join(base, 'link'); symlinkSync(repo, link);
    writeFileSync(join(base, 'file.txt'), 'x');

    const p = await pathInfo(plain, true);
    assert.deepEqual([p.exists, p.dir, p.git, p.repo?.isRepo], [true, true, null, false]);

    const f = await pathInfo(fresh, false);
    assert.equal(f.repo?.isRepo, true, 'a repo with no commits is still a repo');
    assert.deepEqual([f.repo?.hasCommits, f.repo?.branch, f.repo?.detached, f.git?.branch], [false, 'main', false, 'main']);

    const r = await pathInfo(repo, true);
    assert.deepEqual([r.repo?.hasCommits, r.repo?.branch, r.repo?.inSubfolder, r.repo?.root], [true, 'main', false, repo]);
    assert.deepEqual(r.entries, ['sub']);

    const s = await pathInfo(join(repo, 'sub', 'deep'), false);
    assert.deepEqual([s.repo?.inSubfolder, s.repo?.root], [true, repo]);

    const d = await pathInfo(det, false);
    assert.deepEqual([d.repo?.detached, d.repo?.branch, d.git?.branch], [true, '', '']);

    const l = await pathInfo(link, false);
    assert.equal(l.real, repo, 'symlinked project resolves to its target');
    assert.equal(l.repo?.inSubfolder, false);

    const fi = await pathInfo(join(base, 'file.txt'), false);
    assert.deepEqual([fi.exists, fi.dir, fi.repo], [true, false, undefined]);
    assert.deepEqual(await pathInfo(join(base, 'nope'), false), { exists: false, dir: false, git: null });
  } finally { rmSync(base, { recursive: true, force: true }); }
});
