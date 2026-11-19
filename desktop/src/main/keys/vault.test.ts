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

// ---- review 2: K1 (probe on the right key), K2 (one op at a time), K3 (old harness without --stdin), K4 (probe settles)

function mkSwitch(o: { rejects: string[]; delayMs?: number; stdinOk?: (r: string) => boolean }) {
  const dir = mkdtempSync(join(tmpdir(), 'vault-k-'));
  const events: string[] = [];
  let env = '';
  const wait = () => new Promise((r) => setTimeout(r, o.delayMs ?? 0));
  const vault = new KeyVault({ file: join(dir, 'keys.json'), safe: fakeSafe('gnome_libsecret'), root: () => dir, emit: () => {},
    run: async (_r, args) => {
      if (args[0] !== 'unlock') return ok('');
      const seen = env; events.push(`unlock:${seen}`); await wait();
      return o.rejects.includes(seen) ? { code: 1, stdout: '[harness] Upstream rejected the API key\nupstream message: nope', stderr: '', timedOut: false }
        : ok('[harness] upstream API key is OK');
    },
    runStdin: async (_r, _args, input) => { events.push(`write:${input}`); await wait(); env = input; return ok(''); },
    probe: async () => ({ state: 'ok', at: Date.now(), message: 'ok' }), stdinOk: o.stdinOk });
  return { vault, dir, events, env: () => env };
}

test('K1: a rejected switch leaves the previous key\'s probe alone', async () => {
  const { vault, dir } = mkSwitch({ rejects: ['sk-B-0123456789abcdef'] });
  const a = await vault.add({ kind: 'PROXY_API_KEY', label: 'A', secret: 'sk-A-0123456789abcdef', activate: true });
  assert.equal(a.ok, true, JSON.stringify(a));
  const aId = a.state.keys[0].id;
  const b = await vault.add({ kind: 'PROXY_API_KEY', label: 'B', secret: 'sk-B-0123456789abcdef', activate: true });
  assert.equal(b.ok && b.reverted, true, JSON.stringify(b));
  const ka = b.state.keys.find((k) => k.id === aId)!;
  const kb = b.state.keys.find((k) => k.id !== aId)!;
  assert.equal(ka.active, true);
  assert.equal(ka.probe?.state, 'ok', 'the restored key is shown with the new key\'s rejection');
  assert.equal(kb.probe?.state, 'rejected');
  rmSync(dir, { recursive: true, force: true });
});

test('K2: two activations through IPC run one after the other; vault and .env agree', async () => {
  const { vault, dir, events, env } = mkSwitch({ rejects: ['sk-B-0123456789abcdef'], delayMs: 20 });
  const h = new Map<string, (raw: unknown) => Promise<unknown>>();
  const v = { obj: (x: unknown) => x as Record<string, unknown>, str: (x: unknown) => String(x), optStr: (x: unknown) => (x === undefined ? undefined : String(x)),
    oneOf: (x: unknown) => x, none: () => undefined };
  vault.register(((ch: string, check: (r: unknown) => unknown, fn: (p: unknown) => unknown) => { h.set(ch, async (raw) => fn(check(raw))); }) as never, v as never);
  const add = (label: string, secret: string) => h.get('keys:add')!({ kind: 'PROXY_API_KEY', label, secret }) as Promise<{ state: { keys: { id: string; label: string; active: boolean }[] } }>;
  await h.get('keys:add')!({ kind: 'PROXY_API_KEY', label: 'A', secret: 'sk-A-0123456789abcdef', activate: true });
  await add('B', 'sk-B-0123456789abcdef');
  const st = await add('C', 'sk-C-0123456789abcdef');
  const id = (l: string) => st.state.keys.find((k) => k.label === l)!.id;
  events.length = 0;
  const [rb, rc] = await Promise.all([h.get('keys:activate')!({ id: id('B') }), h.get('keys:activate')!({ id: id('C') })]) as { state: { keys: { label: string; active: boolean }[] } }[];
  assert.deepEqual(events, ['write:sk-B-0123456789abcdef', 'unlock:sk-B-0123456789abcdef', 'write:sk-A-0123456789abcdef',
    'write:sk-C-0123456789abcdef', 'unlock:sk-C-0123456789abcdef']);
  assert.ok(rb && rc);
  const active = vault.state().keys.filter((k) => k.active).map((k) => k.label);
  assert.deepEqual(active, ['C']);
  assert.equal(env(), 'sk-C-0123456789abcdef');
  rmSync(dir, { recursive: true, force: true });
});

test('K3: an install whose `config set` has no --stdin is refused before .env is touched', async () => {
  const { stdinSupported } = await import('./vault');
  const d = mkdtempSync(join(tmpdir(), 'vault-k3-'));
  writeFileSync(join(d, 'harness'), '#!/usr/bin/env bash\n_config_set() {\n  local key="$1"; shift\n  local value="$*"\n}\n');
  assert.equal(stdinSupported(d), false);
  const d2 = mkdtempSync(join(tmpdir(), 'vault-k3b-'));
  writeFileSync(join(d2, 'harness'), '#!/usr/bin/env bash\n_config_set() {\n  if [[ "$1" == "--stdin" ]]; then read -r v; fi\n}\n');
  assert.equal(stdinSupported(d2), true);
  const d3 = mkdtempSync(join(tmpdir(), 'vault-k3c-'));
  writeFileSync(join(d3, 'harness'), '#!/usr/bin/env bash\nexec node fake.mjs "$@"\n');
  assert.equal(stdinSupported(d3), true, 'a wrapper without _config_set is not judged');
  const { vault, dir, events } = mkSwitch({ rejects: [], stdinOk: () => false });
  const r = await vault.add({ kind: 'PROXY_API_KEY', label: 'A', secret: 'sk-A-0123456789abcdef', activate: true });
  assert.equal(r.ok, false);
  assert.match(!r.ok ? r.error.message : '', /too old.*update harness/);
  assert.deepEqual(events.filter((e) => e.startsWith('write:')), []);
  for (const x of [d, d2, d3, dir]) rmSync(x, { recursive: true, force: true });
});

test('K4: probeDirect settles when the answer is cut short and when the connect never completes', { timeout: 20000 }, async () => {
  const http = await import('node:http');
  const net = await import('node:net');
  const { probeDirect } = await import('./probe');
  const srv = http.createServer((req, res) => {
    req.resume();
    res.writeHead(401, { 'Content-Type': 'application/json', 'Content-Length': '400' });
    res.write('{"error":{"type":"auth'); setTimeout(() => req.socket.end(), 30);
  });
  await new Promise<void>((r) => srv.listen(0, '127.0.0.1', () => r()));
  const port = (srv.address() as import('node:net').AddressInfo).port;
  const t0 = Date.now();
  const p = await probeDirect(`http://127.0.0.1:${port}`, 'k', 'm', 3000);
  assert.equal(p.state, 'unknown');
  assert.ok(Date.now() - t0 < 2500, `cut answer settled in ${Date.now() - t0} ms`);
  srv.close();
  const hold = net.createServer(() => {});
  await new Promise<void>((r) => hold.listen({ port: 0, host: '127.0.0.1', backlog: 1 }, () => r()));
  const hp = (hold.address() as import('node:net').AddressInfo).port;
  const fillers = Array.from({ length: 8 }, () => net.connect(hp, '127.0.0.1').on('error', () => {}));
  const t1 = Date.now();
  const q = await probeDirect(`http://127.0.0.1:${hp}`, 'k', 'm', 800);
  assert.equal(q.state, 'unknown');
  assert.ok(Date.now() - t1 < 3000, `connect bounded: ${Date.now() - t1} ms`);
  for (const s of fillers) s.destroy();
  hold.close();
});

test('K7: runHarnessStdin timeout ends the whole process group (a forked grandchild too)', { timeout: 20000 }, async () => {
  const dir = mkdtempSync(join(tmpdir(), 'vault-k7-'));
  const pidFile = join(dir, 'gc.pid');
  writeFileSync(join(dir, 'harness'), `sleep 30 & echo $! > ${pidFile}\nwait\n`);
  try {
    const t0 = Date.now();
    const r = await runHarnessStdin(dir, ['config', 'set', 'K', '--stdin'], 'x', { timeoutMs: 300 });
    assert.equal(r.timedOut, true);
    assert.ok(Date.now() - t0 < 5000, `settled in ${Date.now() - t0} ms`);
    const gc = Number(readFileSync(pidFile, 'utf8').trim());
    await new Promise((res) => setTimeout(res, 200));
    assert.throws(() => process.kill(gc, 0), 'the grandchild is gone');
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test('K5: a keys.json entry that is not a key record is dropped on load instead of throwing', () => {
  const dir = mkdtempSync(join(tmpdir(), 'vault-k5-'));
  const file = join(dir, 'keys.json');
  writeFileSync(file, JSON.stringify({ version: 1, storeInactive: false,
    keys: [null, 7, { id: 'a', kind: 'PROXY_API_KEY', label: 'a', enc: null, chars: 20, last4: '', addedAt: 1, active: false }] }));
  try {
    const { vault } = mk('gnome_libsecret', file);
    assert.deepEqual(vault.state().keys.map((k) => k.id), ['a']);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test('K6: a switch while the keyring is only unavailable for now keeps the previous key encrypted', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'vault-k6-'));
  let avail = true;
  const safe = { isEncryptionAvailable: () => avail, getSelectedStorageBackend: () => 'gnome_libsecret',
    encryptString: (s: string) => Buffer.from('X' + s), decryptString: (b: Buffer) => b.toString().slice(1) } as unknown as SafeStorage;
  const vault = new KeyVault({ file: join(dir, 'keys.json'), safe, root: () => dir, emit: () => {},
    run: async (_r, args) => (args[0] === 'unlock' ? ok('[harness] upstream API key is OK') : ok('')),
    runStdin: async () => ok(''), probe: async () => ({ state: 'ok', at: Date.now(), message: 'ok' }), stdinOk: () => true });
  try {
    const a = await vault.add({ kind: 'PROXY_API_KEY', label: 'a', secret: 'sk-aaaaaaaaaaaaaaaaaaaa', activate: true });
    assert.equal(a.ok, true, JSON.stringify(a));
    avail = false;                                              // keyring locked for a moment
    const b = await vault.add({ kind: 'PROXY_API_KEY', label: 'b', secret: 'sk-bbbbbbbbbbbbbbbbbbbb', activate: true });
    assert.equal(b.ok, true, JSON.stringify(b));
    const f = JSON.parse(readFileSync(join(dir, 'keys.json'), 'utf8')) as { keys: { label: string; enc: string | null }[] };
    assert.ok(f.keys.find((k) => k.label === 'a')?.enc, 'the previous key is still stored encrypted');
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
