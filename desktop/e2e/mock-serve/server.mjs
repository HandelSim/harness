// Mock harness serve for e2e (PLAN 7.5). Loopback only. In-process: `await startMock()`; as a process:
// `node cli.mjs --port N --state-id X --scenario S`, steered over POST /__mock/<cmd> (remoteMock).
import http from 'node:http';
import { createHash } from 'node:crypto';
import { scenario as makeScenario, topicPath } from './scenarios.mjs';
// W2-F: canned GET routes (vault tree/file, jira, debug reports, ticket diff) and session messages.
import { VIEW_ROUTES, MESSAGES } from './data/w2f-views.mjs';
// 1x1 PNG served at /vault/file/<rel>.png (the vault image route lives outside /api/v1, like serve's)
const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==', 'base64');
/** Route key of a GET: path, then ?k=v&... with params sorted and sig dropped (unencoded). */
const routeKey = (sub, u) => {
  const q = [...u.searchParams.entries()].filter(([k]) => k !== 'sig').sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  return q.length ? `${sub}?${q.map(([k, v]) => `${k}=${v}`).join('&')}` : sub;
};

const sigOf = (d) => createHash('sha1').update(JSON.stringify(d ?? null)).digest('hex').slice(0, 12);
const TOPIC_RE = /^[a-z-]+(:[A-Za-z0-9._-]+)?$/;

export async function startMock(o = {}) {
  let port = o.port || 0;
  const stateId = o.stateId || 'mockstate0000000';
  let topics = {}; let flags = {};
  let posts = new Map();        // path -> reply
  let failMode = 'none'; let failLeft = 0;
  let reqLog = [];
  let feed = { version: '0.0.1' };
  let nreq = 0;
  let routes = {};              // GET route key -> { status, data } (W2-F views)
  const sockets = new Set();
  const hung = new Set();
  const sessions = new Set();  // valid cookies (codes-on)
  const ACCESS = 'mock-code-1234';

  const setScenario = (name) => {
    const s = makeScenario(name); topics = s.topics; flags = s.flags; posts = new Map(); topics.status.state_id = stateId;
    routes = Object.fromEntries(Object.entries(VIEW_ROUTES).map(([k, d]) => [k, { status: 200, data: d }]));
  };
  setScenario(o.scenario || 'healthy');

  const send = (res, status, body, headers = {}) => {
    const buf = Buffer.from(typeof body === 'string' ? body : JSON.stringify(body));
    res.writeHead(status, { 'Content-Type': typeof body === 'string' ? 'text/plain; charset=utf-8' : 'application/json',
      'Content-Length': buf.length, 'Cache-Control': 'no-store', 'X-Harness-Pid': String(process.pid), ...(flags.stale ? { 'X-Harness-Stale': '1' } : {}), ...headers });
    res.end(buf);
  };
  const authed = (req) => !flags.codes || (req.headers.cookie || '').split(/;\s*/).some((c) => sessions.has(c));

  const control = {
    scenario: async (name) => setScenario(name),
    setTopic: async (topic, data) => { topics[topic] = data; },
    /** W2-F: answer GET <path>[?sorted query] with data (status >= 400: data is the {code,message} error); null removes. */
    setRoute: async (key, data, status = 200) => { if (data === null) delete routes[key]; else routes[key] = { status, data }; },
    onPost: async (path, reply) => { posts.set(path.startsWith('/api/') ? path : `/api/v1${path.startsWith('/') ? '' : '/'}${path}`, reply || {}); },
    fail: async (mode, count) => { failMode = mode; failLeft = mode === 'none' ? 0 : count ?? Infinity; },
    requests: async (filter) => reqLog.filter((r) => !filter || r.path.includes(filter)),
    clearRequests: async () => { reqLog = []; },
    setFeed: async (f) => { feed = { ...feed, ...f }; },
    accessCode: async () => ACCESS,
  };

  async function handle(req, res, body) {
    const u = new URL(req.url, 'http://127.0.0.1');
    const path = u.pathname;
    if (path.startsWith('/__mock/')) {
      const fn = control[path.slice(8)];
      if (!fn) return send(res, 404, { ok: false });
      let args = [];
      try { args = body.length ? JSON.parse(body.toString('utf8')) : []; } catch { /* none */ }
      return send(res, 200, { ok: true, value: (await fn(...(Array.isArray(args) ? args : [args]))) ?? null });
    }
    if (path.startsWith('/feed/')) {
      const name = path.slice(6);
      if (/^latest(-linux(-arm64)?|-mac)?\.yml$/.test(name)) {
        reqLog.push({ method: req.method, path: req.url, headers: { ...req.headers }, body: null });
        return send(res, 200, `version: ${feed.version}\nfiles:\n  - url: Harness-${feed.version}.AppImage\n    sha512: ${'A'.repeat(86)}==\n    size: 1\npath: Harness-${feed.version}.AppImage\nsha512: ${'A'.repeat(86)}==\nreleaseDate: '2026-11-01T00:00:00.000Z'\n`, { 'Content-Type': 'text/yaml' });
      }
      return send(res, 404, 'not found');
    }
    let parsed = null;
    if (body.length) { try { parsed = JSON.parse(body.toString('utf8')); } catch { parsed = body.toString('utf8'); } }
    reqLog.push({ method: req.method, path: req.url, headers: { ...req.headers }, body: parsed });
    if (reqLog.length > 5000) reqLog.splice(0, 1000);
    nreq++;
    if (failLeft > 0 && path !== '/healthz') {
      failLeft--;
      if (failMode === 'drop') { req.socket.destroy(); return; }
      if (failMode === '500') return send(res, 500, { ok: false, error: { code: 'internal', message: 'mock failure' } });
      if (failMode === 'hang') { hung.add(res); return; }
    }
    if (flags.flaky && nreq % 3 === 0 && path !== '/healthz') { req.socket.destroy(); return; }
    if (path === '/healthz') return send(res, 200, 'ok', { 'X-Harness-State': stateId });
    if (path === '/login' && req.method === 'POST') {
      const code = new URLSearchParams(body.toString('utf8')).get('code');
      if (code !== ACCESS) return send(res, 303, '', { Location: '/login?bad=1' });
      const c = `harness_session=s${Math.random().toString(36).slice(2)}`;
      sessions.add(c);
      return send(res, 303, '', { Location: '/', 'Set-Cookie': `${c}; HttpOnly; Path=/; SameSite=Strict` });
    }
    if (path.startsWith('/vault/file/') && req.method === 'GET') {
      if (!authed(req)) return send(res, 401, 'sign in first');
      reqLog.push({ method: req.method, path: req.url, headers: { ...req.headers }, body: null });
      if (!/\.png$/i.test(path)) return send(res, 404, 'not found');
      res.writeHead(200, { 'Content-Type': 'image/png', 'Content-Length': PNG.length, 'Cache-Control': 'no-store' });
      return res.end(PNG);
    }
    if (!path.startsWith('/api/v1/')) return send(res, 200, `<html><body><h1>mock serve ${path}</h1></body></html>`, { 'Content-Type': 'text/html' });
    if (flags.oldServe) return send(res, 404, 'Not Found');
    if (!authed(req)) return send(res, 401, { ok: false, error: { code: 'auth', message: 'sign in first' } });
    const sub = path.slice(7);
    if (req.method === 'GET') {
      if (sub === '/poll') {
        const ts = u.searchParams.getAll('t');
        if (ts.length > 24) return send(res, 400, { ok: false, error: { code: 'bad-request', message: 'at most 24 topics' } });
        const out = {};
        for (const t of ts) {
          const i = t.indexOf('~'); const topic = i < 0 ? t : t.slice(0, i); const sig = i < 0 ? '' : t.slice(i + 1);
          if (!TOPIC_RE.test(topic)) return send(res, 400, { ok: false, error: { code: 'bad-request', message: `bad topic: ${topic}` } });
          if (!(topic in topics)) {
            out[topic] = topic.includes(':') ? { sig: sigOf({ gone: true }), data: { gone: true } } : { error: { code: 'bad-request', message: `unknown topic: ${topic}` } };
            continue;
          }
          const s = sigOf(topics[topic]);
          out[topic] = sig && sig === s ? { sig: s, same: true } : { sig: s, data: topics[topic] };
        }
        return send(res, 200, { ok: true, now: Math.floor(Date.now() / 1000), stale: !!flags.stale, topics: out });
      }
      if (sub === '/opencode') {
        if (!topics.status.opencode) return send(res, 404, { ok: false, error: { code: 'not-found', message: 'no opencode web' } });
        return send(res, 200, { ok: true, sig: 'oc', data: { port: 0, url: topics.status.opencode.url, user: 'opencode', password: 'mock-oc-password' } });
      }
      for (const [t, data] of Object.entries(topics)) {
        if (topicPath(t) === sub) {
          const s = sigOf(data);
          const want = u.searchParams.get('sig');
          return send(res, 200, want && want === s ? { ok: true, sig: s, same: true } : { ok: true, sig: s, data });
        }
      }
      const rk = routeKey(sub, u);
      const hit = routes[rk] ?? routes[sub];
      if (hit) return hit.status >= 400 ? send(res, hit.status, { ok: false, error: hit.data }) : send(res, 200, { ok: true, sig: sigOf(hit.data), data: hit.data });
      const lm = /^\/tickets\/[^/]+\/lines$/.exec(sub);
      if (lm) {
        const from = Math.max(1, Number(u.searchParams.get('from')) || 1); const to = Math.min(from + 499, Number(u.searchParams.get('to')) || from);
        const lines = []; for (let n = from; n <= to; n++) lines.push(`line ${n} of ${u.searchParams.get('path')}`);
        return send(res, 200, { ok: true, sig: sigOf(lines), data: { lines, total: 9999 } });
      }
      const mm = /^\/sessions\/([^/]+)\/messages$/.exec(sub);
      if (mm && (topics.sessions || []).some((r) => r.id === decodeURIComponent(mm[1]))) {
        const from = Math.max(0, Number(u.searchParams.get('from')) || 0);
        const messages = MESSAGES.messages.filter((m) => m.index >= from);
        const data = { ...MESSAGES, from, count: MESSAGES.messages.length, messages };
        return send(res, 200, { ok: true, sig: sigOf(data), data });
      }
      if (sub.startsWith('/tickets/') || sub.startsWith('/sessions/')) return send(res, 404, { ok: false, error: { code: 'not-found', message: 'no such item' } });
      return send(res, 501, { ok: false, error: { code: 'unavailable', message: `mock has no ${sub}` } });
    }
    if (req.method !== 'POST') return send(res, 405, { ok: false, error: { code: 'method', message: 'GET or POST' } });
    const reply = posts.get(path);
    const delay = reply?.delayMs ?? flags.slowMs;
    if (delay) await new Promise((r) => setTimeout(r, delay));
    if (reply?.drop) { req.socket.destroy(); return; }
    if (reply) return send(res, reply.status || 200, reply.body ?? { ok: true, msg: 'ok' });
    if (flags.refuseAll) return send(res, 409, { ok: false, error: { code: 'refused', message: 'refused by the mock' } });
    if (flags.changedOn.includes(path)) return send(res, 409, { ok: false, error: { code: 'changed', message: 'the plan changed: reload it' } });
    return send(res, 200, { ok: true, msg: `${sub.split('/').pop()} done` });
  }

  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => { handle(req, res, Buffer.concat(chunks)).catch((e) => { try { send(res, 500, { ok: false, error: { code: 'internal', message: String(e) } }); } catch { /* gone */ } }); });
  });
  server.on('connection', (s) => { sockets.add(s); s.on('close', () => sockets.delete(s)); });
  const listen = () => new Promise((resolve, reject) => { server.once('error', reject); server.listen(port, '127.0.0.1', () => { port = server.address().port; resolve(); }); });
  await listen();
  const stop = async () => {
    for (const r of hung) { try { r.destroy(); } catch { /* gone */ } }
    hung.clear();
    for (const s of sockets) s.destroy();
    await new Promise((r) => server.close(() => r()));
  };
  return { get url() { return `http://127.0.0.1:${port}`; }, get port() { return port; }, stateId, ...control,
    stop, start: listen, close: stop };
}

/** Remote control of a mock running in another process (spawned by fake-harness). */
export function remoteMock(url) {
  const call = async (cmd, ...args) => {
    const r = await fetch(`${url}/__mock/${cmd}`, { method: 'POST', body: JSON.stringify(args) });
    return (await r.json()).value;
  };
  return { url, scenario: (n) => call('scenario', n), setTopic: (t, d) => call('setTopic', t, d), onPost: (p, r) => call('onPost', p, r),
    setRoute: (k, d, s) => call('setRoute', k, d, s),
    fail: (m, c) => call('fail', m, c), requests: (f) => call('requests', f), setFeed: (f) => call('setFeed', f) };
}
