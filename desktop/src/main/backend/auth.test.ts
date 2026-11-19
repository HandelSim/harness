// Review 2: A1 (an unrecoverable 401 reaches needs-login and never re-posts a refused code), A2 (wall-clock
// deadline including connect, response cap, body cut short), against in-process fake servers.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as http from 'node:http';
import net from 'node:net';
import { tmpdir } from 'node:os';
import type { BackendState } from '../../shared/ipc';
import { API_VERSION } from '../../shared/api';
import { defaultSettings, readEnv } from '../settings';
import { BackendManager } from './manager';
import { ServeClient } from './http';

interface Fake { srv: http.Server; port: number; logins: string[]; valid: Set<string>; limited: boolean }

async function fakeServe(): Promise<Fake> {
  const f = { logins: [] as string[], valid: new Set(['good']), limited: false } as Fake;
  const sessions = new Set<string>();
  f.srv = http.createServer((req, res) => {
    const ck = /sess=([^;]+)/.exec(req.headers.cookie || '')?.[1] || '';
    if (req.url === '/login' && req.method === 'POST') {
      let b = ''; req.on('data', (c) => { b += c; });
      req.on('end', () => {
        const code = new URLSearchParams(b).get('code') || '';
        f.logins.push(code);
        if (f.limited) { res.writeHead(429); res.end('too many'); return; }
        if (!f.valid.has(code)) { res.writeHead(401); res.end('bad code'); return; }
        const s = `s${f.logins.length}-${code}`; sessions.add(s);
        res.writeHead(303, { 'Set-Cookie': `sess=${s}; HttpOnly`, Location: '/' }); res.end();
      });
      return;
    }
    if (req.url === '/healthz') { res.writeHead(200); res.end('ok'); return; }
    const code = ck.split('-').slice(1).join('-');
    if (!sessions.has(ck) || !f.valid.has(code)) { res.writeHead(401, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ ok: false, error: { code: 'auth', message: 'sign in' } })); return; }
    res.writeHead(200, { 'Content-Type': 'application/json' });
    if (req.url?.startsWith('/api/v1/status')) res.end(JSON.stringify({ ok: true, data: { mode: 'serve', api: API_VERSION, pid: 1 } }));
    else if (req.url?.startsWith('/api/v1/poll')) res.end(JSON.stringify({ ok: true, stale: false, topics: {} }));
    else res.end(JSON.stringify({ ok: true, data: {} }));
  });
  await new Promise<void>((r) => f.srv.listen(0, '127.0.0.1', () => r()));
  f.port = (f.srv.address() as net.AddressInfo).port;
  return f;
}

function mk(port: number, code: () => string | null) {
  const settings = defaultSettings();
  const states: BackendState[] = [];
  const saved: string[] = [];
  const m = new BackendManager({
    env: { ...readEnv({}), serveUrl: `http://127.0.0.1:${port}` }, appPath: '/nowhere', packaged: false, home: tmpdir(), logDir: tmpdir(),
    settings: () => settings, patchSettings: () => {}, emit: (s) => states.push(s), live: () => {}, health: () => {},
    accessCode: code, saveAccessCode: (c) => saved.push(c), lastProject: () => null,
  });
  return { m, states, saved };
}

async function until(f: () => boolean, ms = 15000): Promise<void> {
  const t0 = Date.now();
  while (!f()) { if (Date.now() - t0 > ms) throw new Error('timeout'); await new Promise((r) => setTimeout(r, 25)); }
}

test('A1: a code removed while up: one /login post, then needs-login and polling stops', { timeout: 30000 }, async () => {
  const f = await fakeServe();
  const { m } = mk(f.port, () => 'good');
  try {
    m.poller.set('t', ['queue']);
    await m.start();
    assert.equal(m.st.phase, 'up');
    assert.deepEqual(f.logins, ['good']);           // first status GET was 401 -> one login
    f.valid.delete('good');                          // `harness serve code remove`: the session dies with it
    m.poller.kick(0);
    await until(() => m.st.phase === 'needs-login');
    assert.equal(m.st.problem?.code, 'auth');
    const n = f.logins.length;
    assert.equal(n, 2, `one re-login for the dead code, got ${JSON.stringify(f.logins)}`);
    // Renderer GETs and a Retry must not post the refused code again.
    await m.client!.get('/api/v1/queue');
    await m.action({ action: 'retry' });
    assert.equal(m.st.phase, 'needs-login');
    await new Promise((r) => setTimeout(r, 1200));   // a poller still running would have re-posted by now
    assert.equal(f.logins.length, n, `no further posts, got ${JSON.stringify(f.logins)}`);
    // The user types the new code: accepted, the refusal memory is cleared.
    f.valid.add('new');
    await m.action({ action: 'login', code: 'new' });
    assert.equal(m.st.phase, 'up');
  } finally { await m.onQuit(); f.srv.close(); }
});

test('A1: parallel 401s share one /login post (single-flight)', { timeout: 30000 }, async () => {
  const f = await fakeServe();
  const { m } = mk(f.port, () => 'good');
  try {
    await m.start();
    assert.equal(m.st.phase, 'up');
    m.client!.clearCookies();                        // serve restarted: sessions are gone, the code is still valid
    f.logins.length = 0;
    const rs = await Promise.all([1, 2, 3, 4, 5].map(() => m.client!.get('/api/v1/queue')));
    assert.ok(rs.every((r) => r.ok), JSON.stringify(rs));
    assert.equal(f.logins.length, 1, JSON.stringify(f.logins));
  } finally { await m.onQuit(); f.srv.close(); }
});

test('A1: serve answers 429 to a typed code: says the sign-in is blocked, not that the code is wrong', { timeout: 30000 }, async () => {
  const f = await fakeServe();
  const { m } = mk(f.port, () => null);
  try {
    await m.start();
    assert.equal(m.st.phase, 'needs-login');
    assert.equal(f.logins.length, 0, 'no saved code: nothing posted');
    f.limited = true;
    await m.action({ action: 'login', code: 'good' });
    assert.equal(m.st.phase, 'needs-login');
    assert.match(m.st.problem?.message || '', /too many failed sign-ins/);
  } finally { await m.onQuit(); f.srv.close(); }
});

test('A2: a connect that never completes is bounded by the wall-clock deadline', { timeout: 20000 }, async () => {
  // A listener with backlog 1 that never accepts: extra connects sit in SYN state, the idle timer never arms.
  const hold = net.createServer(() => {});
  await new Promise<void>((r) => hold.listen({ port: 0, host: '127.0.0.1', backlog: 1 }, () => r()));
  const port = (hold.address() as net.AddressInfo).port;
  // Fill the backlog with raw sockets that are never read.
  const fillers = Array.from({ length: 8 }, () => net.connect(port, '127.0.0.1').on('error', () => {}));
  try {
    const c = new ServeClient(`http://127.0.0.1:${port}`);
    const t0 = Date.now();
    const r = await c.get('/x', undefined, undefined, 1000);
    assert.equal(r.ok, false);
    assert.ok(Date.now() - t0 < 4000, `settled in ${Date.now() - t0} ms`);
    if (!r.ok) assert.equal(r.error.code, 'timeout');
  } finally { for (const s of fillers) s.destroy(); hold.close(); }
});

test('A2: a response larger than the cap is refused; a body cut short settles', { timeout: 20000 }, async () => {
  const srv = http.createServer((req, res) => {
    if (req.url === '/big') { res.writeHead(200); res.write(Buffer.alloc(300 * 1024)); res.end(); return; }
    res.writeHead(200, { 'Content-Length': '1000' }); res.write('{"ok":tr'); setTimeout(() => req.socket.destroy(), 50);
  });
  await new Promise<void>((r) => srv.listen(0, '127.0.0.1', () => r()));
  const port = (srv.address() as net.AddressInfo).port;
  try {
    const c = new ServeClient(`http://127.0.0.1:${port}`);
    await assert.rejects(c.raw('/big', { maxBytes: 64 * 1024 }), /larger than/);
    const ok = await c.raw('/big');
    assert.equal(ok.body.length, 300 * 1024);
    const t0 = Date.now();
    await assert.rejects(c.raw('/cut', { timeoutMs: 5000 }));
    assert.ok(Date.now() - t0 < 2000, `cut body settled in ${Date.now() - t0} ms`);
  } finally { srv.close(); }
});

test('A4: after a stop, waitDown holds the next start until the old serve stops answering (bounded)', { timeout: 20000 }, async () => {
  const srv = http.createServer((_q, res) => { res.writeHead(200); res.end('ok'); });
  await new Promise<void>((r) => srv.listen(0, '127.0.0.1', () => r()));
  const url = `http://127.0.0.1:${(srv.address() as net.AddressInfo).port}`;
  const { m } = mk(1, () => null);
  const wait = (m as unknown as { waitDown: (u: string | null, s: boolean, ms?: number) => Promise<void> }).waitDown.bind(m);
  let t0 = Date.now();
  await wait(url, false);
  assert.ok(Date.now() - t0 < 100, 'nothing was stopped: no wait');
  setTimeout(() => srv.close(), 800);                // the old serve exits late (after `serve stop` gave up)
  t0 = Date.now();
  await wait(url, true, 5000);
  const took = Date.now() - t0;
  assert.ok(took >= 700 && took < 3000, `waited ${took} ms`);
  t0 = Date.now();
  const hold = http.createServer((_q, res) => { res.writeHead(200); res.end('ok'); });
  await new Promise<void>((r) => hold.listen(0, '127.0.0.1', () => r()));
  await wait(`http://127.0.0.1:${(hold.address() as net.AddressInfo).port}`, true, 600);
  assert.ok(Date.now() - t0 < 2500, 'bounded when it never stops');
  hold.close();
});
