import { test } from 'node:test';
import assert from 'node:assert/strict';
import { findBash } from './bash';

const has = (...ps: string[]) => (p: string) => ps.map((x) => x.toLowerCase()).includes(p.toLowerCase());

test('non-Windows uses bash from PATH', () => {
  assert.equal(findBash({ platform: 'linux', env: {}, exists: () => true }), 'bash');
  assert.equal(findBash({ platform: 'darwin', env: {}, exists: () => false }), 'bash');
});

test('Windows prefers Git Bash under Program Files', () => {
  const b = 'C:\\Program Files\\Git\\bin\\bash.exe';
  assert.equal(findBash({ platform: 'win32', env: { ProgramFiles: 'C:\\Program Files', Path: 'C:\\Windows\\System32' },
    exists: has(b, 'C:\\Windows\\System32\\bash.exe') }), b);
});

test('Windows never picks WSL bash in System32 or WindowsApps', () => {
  const env = { Path: 'C:\\Windows\\System32;C:\\Users\\me\\AppData\\Local\\Microsoft\\WindowsApps' };
  assert.equal(findBash({ platform: 'win32', env, exists: has('C:\\Windows\\System32\\bash.exe',
    'C:\\Users\\me\\AppData\\Local\\Microsoft\\WindowsApps\\bash.exe') }), 'bash');
});

test('Windows finds Git Bash from a git.exe on PATH', () => {
  const env = { Path: 'D:\\tools\\Git\\cmd' };
  assert.equal(findBash({ platform: 'win32', env, exists: has('D:\\tools\\Git\\cmd\\git.exe', 'D:\\tools\\Git\\bin\\bash.exe') }),
    'D:\\tools\\Git\\bin\\bash.exe');
});

test('per-user Git install and HARNESS_DESKTOP_BASH override', () => {
  const u = 'C:\\Users\\me\\AppData\\Local\\Programs\\Git\\bin\\bash.exe';
  assert.equal(findBash({ platform: 'win32', env: { LOCALAPPDATA: 'C:\\Users\\me\\AppData\\Local' }, exists: has(u) }), u);
  assert.equal(findBash({ platform: 'win32', env: { HARNESS_DESKTOP_BASH: 'E:\\b.exe', LOCALAPPDATA: 'C:\\Users\\me\\AppData\\Local' },
    exists: has(u, 'E:\\b.exe') }), 'E:\\b.exe');
});
