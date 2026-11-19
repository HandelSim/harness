import { test } from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { readEnv } from './settings';
import { Updater } from './updater';

class FakeAU extends EventEmitter {
  autoDownload = true; autoInstallOnAppQuit = false; forceDevUpdateConfig = false; logger: unknown = null;
  calls: string[] = [];
  refuse = false;
  setFeedURL(): void {}
  async checkForUpdates() { this.calls.push('check'); this.emit('update-available', { version: '9.9.9', releaseNotes: '' }); return null; }
  async downloadUpdate() { this.calls.push('download'); this.emit('update-downloaded', { version: '9.9.9' }); return []; }
  quitAndInstall(): void { this.calls.push('quitAndInstall'); if (this.refuse) this.emit('error', new Error('No update filepath provided')); }
}

function mk(o: { auto?: boolean } = {}) {
  const au = new FakeAU();
  const order: string[] = [];
  let auto = o.auto ?? true;
  const u = new Updater({ env: { ...readEnv({}), updateUrl: 'http://127.0.0.1:9/feed/' }, packaged: true, version: '1.0.0', emit: () => {},
    auto: () => auto, au: au as never,
    beforeInstall: async () => { order.push('before'); await new Promise((r) => setTimeout(r, 10)); order.push('before-done'); },
    installFailed: () => order.push('failed') });
  au.on('newListener', () => {});
  const origQ = au.quitAndInstall.bind(au);
  au.quitAndInstall = () => { order.push('quitAndInstall'); origQ(); };
  return { u, au, order, setAuto: (b: boolean) => { auto = b; } };
}

test('H2: restart-to-update releases everything before quitAndInstall, once', async () => {
  const { u, au, order } = mk();
  await u.check(); await u.download();
  assert.equal(u.st.phase, 'ready');
  await Promise.all([u.install(), u.install()]);
  assert.deepEqual(order, ['before', 'before-done', 'quitAndInstall']);
  assert.equal(au.calls.filter((c) => c === 'quitAndInstall').length, 1);
});

test('H2: a refused install after the shutdown restarts the current version', async () => {
  const { u, au, order } = mk();
  au.refuse = true;
  await u.check(); await u.download();
  await u.install();
  assert.deepEqual(order, ['before', 'before-done', 'quitAndInstall', 'failed']);
});

test('M10: updates.auto controls background checks and auto-download', async () => {
  const { u, au } = mk({ auto: false });
  u.init();
  assert.equal(u.scheduled(), false);
  await u.check();                                  // a manual check still works
  assert.equal(au.autoDownload, false);
  u.setAuto(true);
  assert.equal(u.scheduled(), true);
  assert.equal(au.autoDownload, true);
  u.setAuto(false);
  assert.equal(u.scheduled(), false);
  assert.equal(au.autoDownload, false);
});

test('N3: a check while an update is ready leaves it ready (no second "Update ready")', async () => {
  const { u, au } = mk();
  await u.check(); await u.download();
  assert.equal(u.st.phase, 'ready');
  const phases: string[] = [];
  const set = (u as unknown as { set: (p: object) => void }).set.bind(u);
  (u as unknown as { set: (p: { phase?: string }) => void }).set = (p) => { if (p.phase) phases.push(p.phase); set(p); };
  await u.check();
  assert.equal(u.st.phase, 'ready');
  assert.deepEqual(phases, []);
  assert.equal(au.calls.filter((c) => c === 'check').length, 1);
});
