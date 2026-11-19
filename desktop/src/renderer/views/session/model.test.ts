import { test } from 'node:test';
import assert from 'node:assert/strict';
import { buildSpec, defaultForm, validate, folderError, ticketError, pushRecentProject, normalizePath, dirName, completions, formFromSpec, needsHostConsent } from './model';

const dirInfo = { exists: true, dir: true, git: { branch: 'main' } };

test('folder validation', () => {
  assert.equal(folderError('', null), 'Choose a folder');
  assert.equal(folderError('rel/path', null), 'Use an absolute path');
  assert.equal(folderError('/x', null), null, 'still checking');
  assert.equal(folderError('/x', { exists: false, dir: false, git: null }), 'Folder does not exist');
  assert.equal(folderError('/x', { exists: true, dir: false, git: null }), 'Not a folder');
  assert.equal(folderError('/x', dirInfo), null);
  assert.equal(folderError('C:\\work', dirInfo), null);
});

test('ticket validation', () => {
  assert.equal(ticketError(''), null);
  assert.equal(ticketError('AB-12'), null);
  assert.equal(ticketError('bad key'), 'Letters, digits, . _ - only');
});

test('host consent gate', () => {
  assert.equal(needsHostConsent('host', false), true);
  assert.equal(needsHostConsent('host', true), false);
  assert.equal(needsHostConsent('agent', false), false);
  const f = { ...defaultForm('host', '/x') };
  assert.deepEqual(validate(f, dirInfo, false, false), { consent: 'Host mode needs your consent' });
  assert.deepEqual(validate(f, dirInfo, false, true), {});
  assert.deepEqual(validate(f, dirInfo, true, false), {});
});

test('buildSpec only sends flags that apply to the kind', () => {
  const f = { ...defaultForm('plain', '/x/'), yolo: true, ticket: 'AB-1', resume: true };
  assert.deepEqual(buildSpec(f), { kind: 'plain', cwd: '/x' });
  const a = { ...defaultForm('agent', '/x'), yolo: true, net: true, ticket: ' AB-1 ', resume: true };
  assert.deepEqual(buildSpec(a), { kind: 'agent', cwd: '/x', yolo: true, net: true, ticket: 'AB-1' });
  const h = { ...defaultForm('host', '/x'), thoughts: true, resume: true };
  assert.deepEqual(buildSpec(h), { kind: 'host', cwd: '/x', thoughts: true, resume: 'last' });
  assert.deepEqual(buildSpec(formFromSpec(buildSpec(h))), buildSpec(h), 'round trip');
});

test('paths and recents', () => {
  assert.equal(normalizePath(' /a/b/ '), '/a/b');
  assert.equal(normalizePath('/'), '/');
  assert.equal(normalizePath('C:\\'), 'C:\\');
  assert.equal(dirName('/a/b'), '/a');
  assert.equal(dirName('/a'), '/');
  assert.deepEqual(pushRecentProject(['/b', '/a'], '/a/'), ['/a', '/b']);
  assert.equal(pushRecentProject(Array.from({ length: 12 }, (_, i) => `/p${i}`), '/new').length, 10);
});

test('completions from recents and the parent folder', () => {
  assert.deepEqual(completions('/home/u/pr', ['proj', 'prod', 'other', '.priv'], []), ['/home/u/proj', '/home/u/prod']);
  assert.deepEqual(completions('/home/u/', ['a'], ['/home/u/x']), ['/home/u/x', '/home/u/a']);
  assert.deepEqual(completions('', [], ['/r1', '/r2']), ['/r1', '/r2']);
});

test('gitCase: each folder case is named (F1)', async () => {
  const { gitCase, ticketBranch } = await import('./model');
  const base = { exists: true, dir: true, git: null };
  const repo = (o: Record<string, unknown>) => ({ ...base, repo: { isRepo: true, root: '/r', branch: 'main', detached: false, hasCommits: true, inSubfolder: false, ...o } });
  assert.equal(gitCase(repo({})).text, 'git repo · main');
  assert.equal(gitCase(repo({})).tone, 'ok');
  assert.match(gitCase(repo({ hasCommits: false })).text, /no commits yet on main/);
  const det = gitCase(repo({ detached: true, branch: '' }));
  assert.match(det.text, /detached HEAD/); assert.equal(det.tone, 'warn');
  const sub = gitCase(repo({ inSubfolder: true }));
  assert.equal(sub.root, '/r'); assert.match(sub.text, /inside the git repo at \/r/);
  assert.equal(gitCase({ ...base, repo: { isRepo: false, root: '', branch: '', detached: false, hasCommits: false, inSubfolder: false, error: 'dubious ownership' } }).text, 'not a git repo (dubious ownership)');
  // legacy shape (main without the repo probe)
  assert.equal(gitCase({ exists: true, dir: true, git: { branch: 'dev' } }).text, 'git repo · dev');
  assert.equal(gitCase(base).repo, false);
  assert.equal(ticketBranch('ABC-1', ['main', 'ABC-1'], []), 'ABC-1');
  assert.equal(ticketBranch('ABC-1', ['main'], ['origin/ABC-1', 'origin/main']), 'origin/ABC-1');
  assert.equal(ticketBranch('ABC-1', ['main'], ['origin/ABC-12']), '');
});
