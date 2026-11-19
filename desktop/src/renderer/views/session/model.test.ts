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
