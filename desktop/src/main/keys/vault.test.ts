import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, readFileSync, chmodSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { SafeStorage } from 'electron';
import { KeyVault, runHarnessStdin, last4Of } from './vault';
import type { RunResult } from '../cli';

const ok = (stdout = ''): RunResult => ({ code: 0, stdout, stderr: '', timedOut: false });
function fakeSafe(backend: string): SafeStorage {
  return { isEncryptionAvailable: () => true, getSelectedStorageBackend: () => backend,
    encryptString: (s: string) => Buffer.from('X' + s), decryptString: (b: Buffer) => b.toString().slice(1) } as unknown as SafeStorage;
}
function mk(backend = 'gnome_libsecret', file?: string) {
  const dir = mkdtempSync(join(tmpdir(), 'vault-'));
  const argv: string[][] = []; const stdin: { args: string[]; input: string }[] = [];
  const vault = new KeyVault({ file: file || join(dir, 'keys.json'), safe: fakeSafe(backend), root: () => dir, emit: () => {},
    run: async (_r, args) => { argv.push(args); return args[0] === 'unlock' ? ok('[harness] upstream API key is OK') : ok(''); },
    runStdin: async (_r, args, input) => { stdin.push({ args, input }); return ok(''); },
    probe: async () => ({ state: 'ok', at: Date.now(), message: 'ok' }) });
  return { vault, dir, argv, stdin, file: file || join(dir, 'keys.json') };
}

test('M8: activating writes the secret through stdin, never on argv', async () => {
  const { vault, argv, stdin, dir } = mk();
  const secret = 'sk-live-ABCDEFGHIJKLMNOP-9876';
  const r = await vault.add({ kind: 'PROXY_API_KEY', label: 'k', secret, activate: true });
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.deepEqual(stdin, [{ args: ['config', 'set', 'PROXY_API_KEY', '--stdin'], input: secret }]);
  assert.ok(!JSON.stringify(argv).includes(secret), 'secret reached argv');
  rmSync(dir, { recursive: true, force: true });
});

test('M8: runHarnessStdin hands the value to the CLI on stdin', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'vault-cli-'));
  writeFileSync(join(dir, 'harness'), '#!/usr/bin/env bash\nprintf "%s|" "$@" > "$(dirname "$0")/argv"; cat > "$(dirname "$0")/got"\n');
  chmodSync(join(dir, 'harness'), 0o755);
  const r = await runHarnessStdin(dir, ['config', 'set', 'K', '--stdin'], 'the secret', { timeoutMs: 10000 });
  assert.equal(r.code, 0, r.stderr);
  assert.equal(readFileSync(join(dir, 'got'), 'utf8'), 'the secret\n');
  assert.equal(readFileSync(join(dir, 'argv'), 'utf8'), 'config|set|K|--stdin|');
  rmSync(dir, { recursive: true, force: true });
});

test('L10: last4 only for secrets of 16+ chars; the length is always given', async () => {
  assert.equal(last4Of('abcd1234'), '');
  assert.equal(last4Of('0123456789abcdef'), 'cdef');
  const { vault, dir } = mk();
  const r = await vault.add({ kind: 'ACCESS_CODE', label: 'c', secret: 'pin-4821' });
  assert.equal(r.ok, true);
  const k = r.state.keys[0];
  assert.equal(k.last4, '');
  assert.equal(k.chars, 8);
  rmSync(dir, { recursive: true, force: true });
});

test('L10: an old keys.json entry loses last4 unless its secret is provably long', () => {
  const dir = mkdtempSync(join(tmpdir(), 'vault-mig-'));
  const file = join(dir, 'keys.json');
  const base = { kind: 'TRACKER_TOKEN', label: 'x', active: false, superseded: false, team: false, addedAt: 1, createdAt: 1, expiresAt: null, lastUnlockAt: null, probe: null };
  writeFileSync(file, JSON.stringify({ version: 1, storeInactive: false, keys: [
    { ...base, id: 'short', last4: '4821', enc: Buffer.from('Xpin-4821').toString('base64') },
    { ...base, id: 'long', last4: 'WXYZ', enc: Buffer.from('X0123456789abcdefWXYZ').toString('base64') },
    { ...base, id: 'gone', last4: '9999', enc: null }] }));
  const { vault } = mk('gnome_libsecret', file);
  const keys = vault.state().keys;
  assert.deepEqual(keys.map((k) => [k.id, k.last4, k.chars ?? null]), [['short', '', 8], ['long', 'WXYZ', 20], ['gone', '', null]]);
  assert.ok(!readFileSync(file, 'utf8').includes('4821'), 'migration not persisted');
  rmSync(dir, { recursive: true, force: true });
});

test('M9: without a keyring an inactive key needs storeInactive; turning it off drops stored secrets from disk', async () => {
  const { vault, file, dir } = mk('basic_text');
  const add = () => vault.add({ kind: 'OAI_API_KEY', label: 'spare', secret: 'sk-spare-0123456789abc' });
  const r0 = await add();
  assert.equal(r0.ok, false);
  assert.equal(!r0.ok && r0.error.code, 'insecure');
  const on = vault.setStoreInactive(true);
  assert.equal(on.ok && on.state.storeInactive, true);
  const r1 = await add();
  assert.equal(r1.ok, true, JSON.stringify(r1));
  const disk = JSON.parse(readFileSync(file, 'utf8'));
  assert.equal(disk.storeInactive, true);
  assert.ok(disk.keys[0].enc, 'inactive key not kept');
  const off = vault.setStoreInactive(false);
  assert.equal(off.ok && off.state.storeInactive, false);
  const disk2 = JSON.parse(readFileSync(file, 'utf8'));
  assert.equal(disk2.keys[0].enc, null);
  // still usable for this run (held in memory)
  const p = await vault.probe({ id: disk2.keys[0].id });
  assert.notEqual(p.message, 'the secret is not stored');
  rmSync(dir, { recursive: true, force: true });
});
