import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import net from 'node:net';
import type { BackendState, Settings } from '../../shared/ipc';
import { defaultSettings, readEnv } from '../settings';
import { BackendManager } from './manager';

const DESKTOP = process.cwd();                    // node:test runs with cwd = desktop/ (scripts/run-tests.mjs)
const FAKE = join(DESKTOP, 'e2e', 'fake-harness');

async function freePort(): Promise<number> {
  return new Promise((res) => { const s = net.createServer(); s.listen(0, '127.0.0.1', () => { const p = (s.address() as net.AddressInfo).port; s.close(() => res(p)); }); });
}

function fakeRoot(fake: object = {}): string {
  const root = mkdtempSync(join(tmpdir(), 'w1a-mgr-'));
  writeFileSync(join(root, 'harness'), readFileSync(join(FAKE, 'harness'), 'utf8').replace('${FAKE_HARNESS_DIR:-$here}', FAKE), { mode: 0o755 });
  mkdirSync(join(root, 'serve')); writeFileSync(join(root, 'serve', 'pub.py'), '');
  writeFileSync(join(root, '.env'), '# fake\n');
  writeFileSync(join(root, 'fake.json'), JSON.stringify(fake));
  return root;
}

function mk(root: string, port: number, onQuit: Settings['backend']['onQuit'] = 'stop') {
  let settings = defaultSettings();
  settings = { ...settings, backend: { ...settings.backend, installRoot: root, port, onQuit } };
  const states: BackendState[] = [];
  const m = new BackendManager({
    env: { ...readEnv({}), root }, appPath: '/nowhere', packaged: false, home: tmpdir(), logDir: join(root, 'logs'),
    settings: () => settings, patchSettings: (p) => { settings = { ...settings, backend: { ...settings.backend, ...p.backend } }; },
    emit: (s) => { states.push(s); if (process.env.DEBUG_W1A) console.error('state', s.phase, s.port, s.owned, s.problem?.message); }, live: () => {}, health: () => {}, accessCode: () => null, saveAccessCode: () => {}, lastProject: () => null,
  });
  return { m, states };
}

const serveCalls = (root: string) => (JSON.parse(readFileSync(join(root, 'fake.json'), 'utf8')).calls as string[][]).filter((c) => c[0] === 'serve' && c[1] === '--port').length;
const alive = (pid: number) => { try { process.kill(pid, 0); return true; } catch { return false; } };
async function until(f: () => boolean, ms = 20000): Promise<void> {
  const t0 = Date.now();
  while (!f()) { if (Date.now() - t0 > ms) throw new Error('timeout'); await new Promise((r) => setTimeout(r, 50)); }
}

test('H1: a retry while serve is starting joins the start; one serve, owned, stopped on quit', { timeout: 60000 }, async () => {
  const root = fakeRoot();
  const port = await freePort();
  const { m } = mk(root, port);
  try {
    const first = m.start();
    await until(() => m.st.phase === 'starting' && m.spawned().length === 1);
    const second = m.action({ action: 'retry' });
    const third = m.action({ action: 'start' });
    await Promise.all([first, second, third]);
    assert.equal(m.st.phase, 'up');
    assert.equal(m.st.owned, true);
    assert.equal(serveCalls(root), 1);
    const [pid] = m.spawned();
    assert.ok(pid && alive(pid));
    await m.onQuit();
    await until(() => !alive(pid), 10000);
    assert.deepEqual(m.spawned(), []);
  } finally { await m.onQuit().catch(() => {}); rmSync(root, { recursive: true, force: true }); }
});

test('H1: create-env during a start adopts the spawned serve instead of dead-ending in locating', { timeout: 60000 }, async () => {
  const root = fakeRoot();
  const port = await freePort();
  const { m } = mk(root, port);
  try {
    const first = m.start();
    await until(() => m.spawned().length === 1);
    await m.action({ action: 'create-env' });           // cancels the in-flight start and starts again
    await first;
    assert.equal(m.st.phase, 'up');
    assert.equal(m.st.owned, true);
    assert.equal(serveCalls(root), 1);
    assert.equal(m.spawned().length, 1);
    assert.ok(m.st.logTail.some((l) => l.includes('serve on')), 'logTail comes from serve.log');
    assert.ok(existsSync(join(root, 'logs', 'serve.log')));
    assert.equal(statSync(join(root, 'logs', 'serve.log')).mode & 0o777, 0o600);
  } finally { await m.onQuit().catch(() => {}); rmSync(root, { recursive: true, force: true }); }
});

test('L15: an async spawn error ends in down with the message, and a retry is not a dead end', { timeout: 60000 }, async () => {
  const root = fakeRoot();
  const port = await freePort();
  const { m } = mk(root, port);
  const oldPath = process.env.PATH;
  try {
    process.env.PATH = '/nonexistent-w1a';           // `bash` cannot be found: spawn emits 'error', never 'exit'
    await m.start();
    process.env.PATH = oldPath;
    assert.equal(m.st.phase, 'down');
    assert.match(m.st.problem?.message || '', /could not start harness serve|ENOENT/);
    assert.deepEqual(m.spawned(), []);
    await m.action({ action: 'retry' });
    assert.equal(m.st.phase, 'up');
  } finally { process.env.PATH = oldPath; await m.onQuit().catch(() => {}); rmSync(root, { recursive: true, force: true }); }
});

test('L3: with the port taken by another install, a serve of ours on a fallback port is attached, not spawned again', { timeout: 60000 }, async () => {
  const root = fakeRoot();
  const port = await freePort();
  const other = net.createServer((s) => s.end('HTTP/1.1 200 OK\r\nx-harness-state: someone-else\r\ncontent-length: 0\r\n\r\n'));
  await new Promise<void>((r) => other.listen(port, '127.0.0.1', () => r()));
  const a = mk(root, port, 'keep');
  let keptPid = 0;
  try {
    await a.m.start();
    assert.equal(a.m.st.phase, 'up');
    assert.ok(a.m.st.port! >= port + 10);
    [keptPid] = a.m.spawned();
    await a.m.onQuit();                                 // 'keep': serve stays up
    assert.ok(alive(keptPid));
    const b = mk(root, port, 'stop');
    try {
      await b.m.start();
      assert.equal(b.m.st.phase, 'up');
      assert.equal(b.m.st.port, a.m.st.port);
      assert.equal(serveCalls(root), 1);
    } finally { await b.m.onQuit(); }               // attached, not spawned by b: left running
    assert.ok(alive(keptPid));
  } finally {
    other.close();
    if (keptPid) { try { process.kill(-keptPid, 'SIGTERM'); } catch { /* gone */ } }
    rmSync(root, { recursive: true, force: true });
  }
});

test('L11: create-env writes .env with mode 0600', { timeout: 30000 }, async () => {
  const root = fakeRoot({ env: 'missing' });
  rmSync(join(root, '.env'));
  writeFileSync(join(root, '.env.example'), 'X=1\n', { mode: 0o644 });
  const port = await freePort();
  const { m } = mk(root, port);
  try {
    await m.start();
    assert.equal(m.st.phase, 'needs-config');
    await m.action({ action: 'create-env' });
    assert.equal(statSync(join(root, '.env')).mode & 0o777, 0o600);
  } finally { await m.onQuit().catch(() => {}); rmSync(root, { recursive: true, force: true }); }
});
