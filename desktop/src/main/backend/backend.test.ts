import { test } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { createHash } from 'node:crypto';
import { classify, extractUnlockUrl } from './classify';
import { locateRoot, stateId, type Fs } from './locate';
import { ServeClient } from './http';
import { Poller, BACKOFF, pollPath, type Clock, type PollAnswer } from './poller';

test('classify: the CLI failure texts', () => {
  assert.deepEqual(classify('ERROR: required value(s) empty in /r/.env: PROXY_API_KEY DEFAULT_MODEL_NAME').code, 'needs-config');
  const c = classify('x\nERROR: required value(s) empty in /r/.env: PROXY_API_KEY DEFAULT_MODEL_NAME\n');
  assert.deepEqual(c.code === 'needs-config' && c.missing, ['PROXY_API_KEY', 'DEFAULT_MODEL_NAME']);
  assert.equal(classify('ERROR: missing /r/.env').code, 'needs-config');
  const l = classify(' ERROR: Upstream API key is locked\n Visit this URL in your browser to unlock the key:\n   https://u.example/k/1\n');
  assert.equal(l.code, 'key-locked');
  assert.equal(l.code === 'key-locked' && l.unlockUrl, 'https://u.example/k/1');
  assert.equal(classify('ERROR: Upstream rejected the API key (HTTP 401)').code, 'key-rejected');
  assert.equal(classify('OSError: [Errno 98] Address already in use').code, 'port-in-use');
  assert.equal(classify('bash: python3: command not found').code, 'python-missing');
  assert.equal(classify('something else broke').code, 'other');
  assert.equal(extractUnlockUrl('no url here'), undefined);
});

test('locateRoot: env, then settings, then the repo around the app, then PATH', () => {
  const ok = new Set(['/env/harness', '/env/serve/pub.py', '/set/harness', '/set/serve/pub.py']);
  const fs: Fs = { isExec: (p) => ok.has(p), exists: (p) => ok.has(p), realpath: (p) => p };
  const base = { envRoot: null, settingsRoot: null, appPath: '/nowhere/desktop', packaged: false, pathEnv: '', home: '/home/u' };
  assert.equal(locateRoot({ ...base, envRoot: '/env' }, fs).root, '/env');
  assert.equal(locateRoot({ ...base, settingsRoot: '/set' }, fs).root, '/set');
  assert.equal(locateRoot(base, fs).root, null);
});

test('stateId matches pub.py state_id (sha256 of the realpath, 16 hex)', () => {
  const fs: Fs = { isExec: () => true, exists: () => true, realpath: (p) => p.replace('/link', '/real') };
  assert.equal(stateId('/link', fs), createHash('sha256').update('/real/state').digest('hex').slice(0, 16));
});

test('stateId on Windows applies ntpath.normcase (lowercase, backslashes) like pub.py', () => {
  const fs: Fs = { isExec: () => true, exists: () => true, realpath: (p) => p };
  assert.equal(stateId('C:\\Users\\Me\\harness', fs, 'win32'),
    createHash('sha256').update('c:\\users\\me\\harness\\state').digest('hex').slice(0, 16));
});

function server(handler: http.RequestListener): Promise<{ url: string; close(): void }> {
  return new Promise((res) => {
    const s = http.createServer(handler);
    s.listen(0, '127.0.0.1', () => res({ url: `http://127.0.0.1:${(s.address() as { port: number }).port}`, close: () => s.close() }));
  });
}

test('ServeClient: envelope, no browser headers, 401 relogin once for GET, POST never retried', async () => {
  const seen: http.IncomingHttpHeaders[] = [];
  let posts = 0; let gets = 0;
  const s = await server((req, res) => {
    seen.push(req.headers);
    if (req.method === 'POST') { posts++; res.writeHead(401, { 'Content-Type': 'application/json' }); res.end('{"ok":false,"error":{"code":"auth","message":"x"}}'); return; }
    gets++;
    if (gets === 1) { res.writeHead(401, { 'Content-Type': 'application/json' }); res.end('{"ok":false,"error":{"code":"auth","message":"x"}}'); return; }
    res.writeHead(200, { 'Content-Type': 'application/json' }); res.end('{"ok":true,"sig":"s1","data":{"n":1}}');
  });
  const c = new ServeClient(s.url);
  let relogins = 0;
  c.relogin = async () => { relogins++; return true; };
  const r = await c.get<{ n: number }>('/api/v1/queue');
  assert.equal(r.ok, true);
  assert.equal(r.ok && r.data?.n, 1);
  assert.equal(relogins, 1);
  const p = await c.post('/api/v1/queue/start', {});
  assert.equal(p.ok, false);
  assert.equal(posts, 1);
  for (const h of seen) { assert.equal(h.origin, undefined); assert.equal(h.referer, undefined); assert.equal(h['sec-fetch-site'], undefined); }
  s.close();
});

test('ServeClient: offline, and a dropped POST is unknown-result', async () => {
  const s = await server((req) => { req.on('data', () => {}); req.on('end', () => req.socket.destroy()); });
  const c = new ServeClient(s.url);
  const p = await c.post('/api/v1/queue/halt', {});
  assert.equal(p.ok, false);
  assert.equal(!p.ok && p.error.code, 'unknown-result');
  s.close();
  const off = await new ServeClient('http://127.0.0.1:1').get('/api/v1/status');
  assert.equal(!off.ok && off.error.code, 'offline');
});

class FakeClock implements Clock {
  t = 0; q: { at: number; fn: () => void; id: number }[] = []; n = 0;
  now() { return this.t; }
  set(fn: () => void, ms: number) { const id = ++this.n; this.q.push({ at: this.t + ms, fn, id }); return id; }
  clear(h: unknown) { this.q = this.q.filter((x) => x.id !== h); }
  async advance(ms: number) {
    const end = this.t + ms;
    for (;;) {
      this.q.sort((a, b) => a.at - b.at);
      const nx = this.q[0];
      if (!nx || nx.at > end) break;
      this.q.shift(); this.t = nx.at; nx.fn();
      for (let i = 0; i < 5; i++) await Promise.resolve();
    }
    this.t = end;
  }
}

test('Poller: unions owners, sends sigs, emits only on change, backs off on failure', async () => {
  const clock = new FakeClock();
  const calls: { topic: string; sig: string }[][] = [];
  const updates: string[] = [];
  const health: [boolean, number][] = [];
  let answer: PollAnswer = { ok: true, stale: false, topics: { queue: { sig: 'a', data: { runner: { running: false } } }, status: { sig: 'b', data: {} } } };
  const p = new Poller({ clock, fetch: async (q) => { calls.push(q); return answer; }, update: (t, s) => updates.push(`${t}~${s}`),
    health: (ok, f) => health.push([ok, f]) });
  p.set('view', ['queue']); p.set('bar', ['status', 'queue']);
  p.start();
  await clock.advance(0);
  assert.deepEqual(calls[0].map((x) => x.topic).sort(), ['queue', 'status']);
  assert.deepEqual(updates.sort(), ['queue~a', 'status~b']);
  answer = { ok: true, stale: false, topics: { queue: { sig: 'a', same: true }, status: { sig: 'b', same: true } } };
  await clock.advance(3000);
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[1].find((x) => x.topic === 'queue')?.sig, 'a');
  assert.equal(updates.length, 2, 'same -> no emit');
  answer = { ok: false, status: 0 };
  await clock.advance(3000);
  assert.equal(p.failures, 1);
  assert.equal(p.interval(), BACKOFF[0]);
  await clock.advance(BACKOFF[0]);
  assert.equal(p.interval(), BACKOFF[1]);
  p.setVisibility('hidden');
  p.set('view', ['queue', 'ticket:A-1']);
  assert.ok(!p.topics().includes('ticket:A-1'), 'hidden polls status and queue only');
  p.stop();
});

test('Poller cadence: busy 1 s, focused 3 s, blurred 5 s, hidden 15 s', async () => {
  const clock = new FakeClock();
  const p = new Poller({ clock, fetch: async () => ({ ok: true, stale: false, topics: { queue: { sig: 'r', data: { runner: { running: true } } } } }),
    update: () => {}, health: () => {} });
  p.set('o', ['queue']); p.start(); await clock.advance(0);
  assert.equal(p.interval(), 1000);
  p.setVisibility('blurred'); assert.equal(p.interval(), 2000, 'busy stays fast while blurred (L13)');
  p.setVisibility('hidden'); assert.equal(p.interval(), 15000);
  p.stop();
  assert.match(pollPath([{ topic: 'queue', sig: 'x' }, { topic: 'ticket:A-1', sig: '' }]), /^\/api\/v1\/poll\?t=queue~x&t=ticket(:|%3A)A-1~$/);
});
