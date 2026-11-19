// Backend manager (1.2): locate -> config check -> attach or spawn -> up, with reconnect, respawn limits,
// stale detection, access-code login and quit policy.
import { spawn, type ChildProcess } from 'node:child_process';
import { chmodSync, closeSync, copyFileSync, existsSync, fstatSync, mkdirSync, openSync, renameSync, statSync } from 'node:fs';
import { open } from 'node:fs/promises';
import { StringDecoder } from 'node:string_decoder';
import { hostname } from 'node:os';
import { join } from 'node:path';
import { API_VERSION, type Queue, type Status } from '../../shared/api';
import type { BackendAction, BackendState, Settings } from '../../shared/ipc';
import { backendUrlOk } from '../security';
import { runHarness, parseConfigTsv, missingRequired, cliEnv } from '../cli';
import { log } from '../log';
import type { Env } from '../settings';
import { classify } from './classify';
import { ServeClient } from './http';
import { locateRoot, stateId, validRoot } from './locate';
import { Poller, pollPath, type PollAnswer } from './poller';

export interface ManagerDeps {
  env: Env; appPath: string; packaged: boolean; home: string; logDir: string;
  settings(): Settings; patchSettings(p: { backend: Partial<Settings['backend']> }): void;
  emit(s: BackendState): void;
  live(topic: string, sig: string, data: unknown): void;
  health(ok: boolean, failures: number, nextInMs: number): void;
  accessCode(): string | null; saveAccessCode(code: string): void;
  lastProject(): string | null;
  /** consent-host: a native confirmation in main (L8); absent = allowed (tests). */
  confirmHost?(): Promise<boolean>;
}

const RING = 400;
const READY_MS = 120000;
const RESPAWN = [1000, 5000, 15000];
const LOG_ROTATE = 5 * 1024 * 1024;
const STOP_CLI_MS = 5000;

export class BackendManager {
  st: BackendState = { phase: 'locating', root: null, url: null, port: null, owned: false, pid: null, stale: false,
    apiVersion: null, missing: [], hostConsent: false, problem: null, logTail: [], since: Date.now() };
  client: ServeClient | null = null;
  status: Status | null = null;
  poller: Poller;
  private child: ChildProcess | null = null;   // the serve this app spawned and is waiting on / watching
  private childPort = 0;
  private children = new Set<ChildProcess>();  // every spawned serve not yet seen to exit: all are stopped on quit
  private inflight: Promise<void> | null = null;
  private logFile = '';
  private logOff = 0;
  private logAll = '';                          // output since the last spawn (classify input), capped
  private logPartial = '';
  private logDec = new StringDecoder('utf8');
  private tailing: Promise<void> = Promise.resolve();
  private respawns: number[] = [];
  private skipProbe = false;
  private gen = 0;              // bumps on every (re)start so stale async work is dropped
  private stopping = false;
  private respawnTimer: NodeJS.Timeout | null = null;
  private lastQueue: Queue | null = null;

  constructor(private d: ManagerDeps) {
    this.poller = new Poller({
      fetch: (q) => this.pollFetch(q),
      update: (t, sig, data) => { if (t === 'queue') this.lastQueue = data as Queue; d.live(t, sig, data); },
      health: (ok, failures, next) => { d.health(ok, failures, next); this.onHealth(ok, failures); },
      stale: (s) => this.setStale(s),
    });
  }

  get(): BackendState { return { ...this.st, logTail: [...this.st.logTail] }; }

  private set(p: Partial<BackendState>): void {
    if (p.phase && p.phase !== this.st.phase) p.since = Date.now();
    this.st = { ...this.st, ...p, hostConsent: this.d.settings().backend.hostConsent };
    this.d.emit(this.get());
  }

  private setStale(s: boolean): void { if (s !== this.st.stale) this.set({ stale: s }); }

  /** Single-flight: a start while one is in flight joins it (H1). Use fresh() to restart with new inputs. */
  start(): Promise<void> {
    if (this.inflight) return this.inflight;
    const p: Promise<void> = this.startNow().finally(() => { if (this.inflight === p) this.inflight = null; });
    this.inflight = p;
    return p;
  }

  /** Cancel an in-flight start (its async steps drop out on the gen check), wait for it, then start again. */
  private async fresh(): Promise<void> {
    this.gen++;
    if (this.inflight) await this.inflight.catch(() => undefined);
    await this.start();
  }

  private async startNow(): Promise<void> {
    const g = ++this.gen;
    this.stopping = false;
    this.set({ phase: 'locating', problem: null, missing: [] });
    const env = this.d.env;
    if (env.serveUrl) {
      if (!backendUrlOk(env.serveUrl)) {
        this.set({ phase: 'down', problem: { code: 'bad-url', message: 'HARNESS_DESKTOP_SERVE_URL must be http://127.0.0.1:<port> or http://localhost:<port>' } });
        return;
      }
      const loc = locateRoot(this.locIn());
      this.set({ root: loc.root });
      await this.attach(env.serveUrl, false, g);
      return;
    }
    const loc = locateRoot(this.locIn());
    if (!loc.root) { this.set({ phase: 'no-install', root: null, problem: { code: 'no-install', message: `no harness install found (looked in ${loc.tried.join(', ')})` } }); return; }
    const root = loc.root;
    this.set({ root });
    if (!existsSync(join(root, '.env'))) { this.set({ phase: 'needs-config', missing: ['.env'] }); return; }
    const cfg = await runHarness(root, ['config', 'list', '--tsv'], { timeoutMs: 30000 });
    if (g !== this.gen) return;
    if (cfg.code === 0) {
      const missing = missingRequired(parseConfigTsv(cfg.stdout));
      if (missing.length) { this.set({ phase: 'needs-config', missing }); return; }
    }
    const port = env.servePort ?? this.d.settings().backend.port;
    await this.attachOrSpawn(root, port, g);
  }

  private locIn() {
    return { envRoot: this.d.env.root, settingsRoot: this.d.settings().backend.installRoot, appPath: this.d.appPath,
      packaged: this.d.packaged, pathEnv: process.env.PATH || '', home: this.d.home };
  }

  private async attachOrSpawn(root: string, port: number, g: number): Promise<void> {
    const want = stateId(root);
    const url = `http://127.0.0.1:${port}`;
    const h = await healthz(url);
    if (g !== this.gen) return;
    if (h && h.state === want) {
      const c = new ServeClient(url);
      const s = await c.get<Status>('/api/v1/status', undefined, undefined, 5000);
      if (g !== this.gen) return;
      if (s.ok && s.data?.mode === 'serve') { await this.attach(url, this.ownsPort(port), g); return; }
      if (!s.ok && s.status === 404) {
        this.set({ phase: 'incompatible', url, port, problem: { code: 'old-serve', message: 'the running harness serve predates the app API: restart the backend' } });
        return;
      }
      if (!s.ok && s.error.code === 'auth') { await this.attach(url, this.ownsPort(port), g); return; }
      // viewer (or anything else of ours): harness serve takes the port over by itself
      await this.spawnServe(root, port, g);
      return;
    }
    if (h) {
      // another install owns the port. Ours may already run on a fallback port (kept from an earlier launch, L3):
      // attach to it instead of spawning a second serve for the same state; else spawn on the next free one.
      const alt = await findServe(port + 10, 10, want);
      if (g !== this.gen) return;
      if (alt) { await this.attach(`http://127.0.0.1:${alt}`, this.ownsPort(alt), g); return; }
      const free = this.child ? this.childPort : await findFreePort(port + 10);
      if (g !== this.gen) return;
      await this.spawnServe(root, free, g);
      return;
    }
    await this.spawnServe(root, port, g);
  }

  /** Attach to a running serve: status, api version, auth. */
  private async attach(url: string, owned: boolean, g: number): Promise<boolean> {
    const port = Number(new URL(url).port);
    if (!this.client || this.client.base !== url) {
      this.client = new ServeClient(url);
      this.client.relogin = () => this.relogin();
      this.client.onHeaders = (hd) => { if (hd['x-harness-stale'] === '1') this.setStale(true); };
      this.poller.reset();
    }
    this.set({ phase: owned ? 'starting' : this.st.phase === 'reconnecting' ? 'reconnecting' : 'starting', url, port, owned });
    let s = await this.client.get<Status>('/api/v1/status', undefined, undefined, 5000);
    if (g !== this.gen) return false;
    if (!s.ok && s.error.code === 'auth') {
      const ok = await this.relogin();
      if (g !== this.gen) return false;
      if (!ok) { this.set({ phase: 'needs-login', problem: { code: 'auth', message: 'This backend asks for an access code' } }); return false; }
      s = await this.client.get<Status>('/api/v1/status', undefined, undefined, 5000);
      if (g !== this.gen) return false;
    }
    if (!s.ok) {
      if (s.status === 404) { this.set({ phase: 'incompatible', problem: { code: 'old-serve', message: 'the running harness serve predates the app API: restart the backend' } }); return false; }
      this.set({ phase: 'down', problem: { code: s.error.code, message: s.error.message } });
      if (!owned) this.scheduleReprobe(g);
      return false;
    }
    const status = s.data as Status;
    this.status = status;
    if (status.api !== API_VERSION) {
      this.set({ phase: 'incompatible', apiVersion: status.api, problem: status.api < API_VERSION
        ? { code: 'update-harness', message: 'this harness is older than the app: update harness' }
        : { code: 'update-app', message: 'this harness is newer than the app: update the app' } });
      return false;
    }
    this.set({ phase: 'up', apiVersion: status.api, pid: status.pid ?? this.st.pid, stale: !!status.stale,
      problem: this.skipProbe ? this.st.problem : null });
    this.poller.start();
    this.poller.kick(0);
    return true;
  }

  private reprobeTimer: NodeJS.Timeout | null = null;
  private scheduleReprobe(g: number, ms = 2000): void {
    if (this.reprobeTimer) clearTimeout(this.reprobeTimer);
    this.reprobeTimer = setTimeout(() => {
      this.reprobeTimer = null;
      if (g === this.gen && this.st.url && (this.st.phase === 'down' || this.st.phase === 'reconnecting')) void this.attach(this.st.url, this.st.owned, g);
    }, ms);
  }

  private async relogin(): Promise<boolean> {
    const code = this.d.accessCode();
    if (!code || !this.client) return false;
    try { return await this.client.login(code); } catch { return false; }
  }

  private onHealth(ok: boolean, failures: number): void {
    if (!ok && failures >= 3 && this.st.phase === 'up') this.set({ phase: 'reconnecting' });
    if (ok && this.st.phase === 'reconnecting' && this.st.url) void this.attach(this.st.url, this.st.owned, this.gen);
  }

  private async pollFetch(q: { topic: string; sig: string }[]): Promise<PollAnswer> {
    if (!this.client || (this.st.phase !== 'up' && this.st.phase !== 'reconnecting')) return { ok: false, status: 0 };
    try {
      const r = await this.client.raw(pollPath(q), { timeoutMs: 10000 });
      if (r.status === 401 && await this.relogin()) return { ok: false, status: 401 };
      if (r.status !== 200) return { ok: false, status: r.status };
      const j = JSON.parse(r.body.toString('utf8'));
      if (!j || j.ok !== true || typeof j.topics !== 'object') return { ok: false, status: r.status };
      const pid = Number(r.headers['x-harness-pid'] || 0);
      if (pid && this.st.pid && pid !== this.st.pid) this.set({ pid });
      return { ok: true, stale: !!j.stale, topics: j.topics };
    } catch { return { ok: false, status: 0 }; }
  }

  private ownsPort(port: number): boolean { return !!this.child && this.childPort === port; }

  /** Spawn serve, or adopt the one a cancelled start already spawned (H1: never a second child, never a dead end). */
  private async spawnServe(root: string, port: number, g: number): Promise<void> {
    let child = this.child;
    if (child) port = this.childPort;
    this.set({ phase: 'starting', owned: true, port, url: `http://127.0.0.1:${port}`, ...(child ? {} : { logTail: [] }) });
    if (!child) {
      const c = this.spawnChild(root, port);
      if (!c) return;
      child = c;
    }
    const url = `http://127.0.0.1:${port}`;
    const t0 = Date.now();
    const mine = () => this.child === child && g === this.gen;
    while (mine() && Date.now() - t0 < READY_MS) {
      await sleep(250);
      await this.tailLog();
      if (!mine()) return;
      const h = await healthz(url);
      if (!h || !mine()) continue;
      if (await this.attach(url, true, g)) return;
      if (this.st.phase !== 'starting' && this.st.phase !== 'down') return;
    }
    if (mine() && this.st.phase === 'starting') {
      this.set({ phase: 'down', problem: { code: 'timeout', message: 'harness serve did not come up in 120 s' } });
    }
  }

  /** The child writes to serve.log directly (no pipes into this process: a kept serve outlives the app, L2);
   * the log is tailed for logTail while starting and on exit. */
  private spawnChild(root: string, port: number): ChildProcess | null {
    const extra: Record<string, string> = { HARNESS_OPEN: '0' };
    if (this.d.settings().backend.hostConsent) extra.HARNESS_HOST_CONFIRM = '1';
    if (this.skipProbe) extra.HARNESS_SKIP_AUTH_PROBE = '1';
    const cwd = this.d.lastProject() || this.d.home;
    let fd = -1;
    let child: ChildProcess;
    try {
      mkdirSync(this.d.logDir, { recursive: true });
      this.logFile = join(this.d.logDir, 'serve.log');
      try { if (statSync(this.logFile).size > LOG_ROTATE) renameSync(this.logFile, this.logFile + '.1'); } catch { /* none yet */ }
      fd = openSync(this.logFile, 'a', 0o600);
      this.logOff = fstatSync(fd).size;
      this.logAll = ''; this.logPartial = ''; this.logDec = new StringDecoder('utf8');
      child = spawn('bash', [join(root, 'harness'), 'serve', '--port', String(port)], {
        cwd: existsSync(cwd) ? cwd : this.d.home, env: cliEnv(extra), stdio: ['ignore', fd, fd], detached: true });
    } catch (e) {
      this.set({ phase: 'down', owned: false, problem: { code: 'spawn', message: String(e) } });
      return null;
    } finally { if (fd >= 0) { try { closeSync(fd); } catch { /* ignore */ } } }
    this.child = child;
    this.childPort = port;
    this.children.add(child);
    this.set({ pid: child.pid ?? null });
    let done = false;
    const gone = (code: number | null, err?: Error) => {
      if (done) return;
      done = true;
      this.children.delete(child);
      if (this.child !== child) return;          // stopped, or replaced: whoever replaced it owns the state
      this.child = null;
      if (this.stopping) return;
      const g = this.gen;
      void this.tailLog().then(() => {
        if (g !== this.gen || this.stopping) return;
        if (err) {                                 // async spawn failure (ENOENT, EAGAIN, ...): no 'exit' follows (L15)
          log('warn', `serve spawn failed: ${err.message}`);
          this.set({ phase: 'down', owned: false, pid: null, problem: { code: 'spawn', message: `could not start harness serve: ${err.message}` } });
          return;
        }
        log('warn', `owned serve exited with ${code}`);
        if (this.st.phase === 'starting') { void this.failed(root, port, this.logAll, g); return; }
        this.set({ phase: 'reconnecting', pid: null });
        this.scheduleRespawn(root, port, g);
      });
    };
    child.on('exit', (code) => gone(code));
    child.on('error', (e) => gone(null, e));
    return child;
  }

  /** Read what serve appended to serve.log since the last read (serialised; never blocks main on sync I/O). */
  private tailLog(): Promise<void> {
    this.tailing = this.tailing.then(() => this.tailOnce()).catch(() => undefined);
    return this.tailing;
  }

  private async tailOnce(): Promise<void> {
    if (!this.logFile) return;
    const fh = await open(this.logFile, 'r').catch(() => null);
    if (!fh) return;
    try {
      const { size } = await fh.stat();
      if (size < this.logOff) this.logOff = 0;
      if (size === this.logOff) return;
      const n = Math.min(size - this.logOff, 512 * 1024);
      const buf = Buffer.alloc(n);
      const { bytesRead } = await fh.read(buf, 0, n, this.logOff);
      this.logOff += bytesRead;
      const s = this.logDec.write(buf.subarray(0, bytesRead));
      if (this.logAll.length < 200000) this.logAll += s;
      const lines = (this.logPartial + s).split('\n');
      this.logPartial = lines.pop() || '';
      if (lines.length) {
        const tail = [...this.st.logTail, ...lines.map((l) => l.replace(/\x1b\[[0-9;?]*[A-Za-z]/g, ''))].slice(-RING);
        this.st = { ...this.st, logTail: tail };
        if (this.st.phase === 'starting') this.d.emit(this.get());
      }
    } finally { await fh.close().catch(() => undefined); }
  }

  private async failed(root: string, port: number, out: string, g: number): Promise<void> {
    const f = classify(out);
    if (f.code === 'needs-config') { this.set({ phase: 'needs-config', owned: false, missing: f.missing, problem: { code: f.code, message: f.message } }); return; }
    if ((f.code === 'key-locked' || f.code === 'key-rejected') && !this.skipProbe) {
      this.skipProbe = true;
      this.set({ problem: { code: f.code, message: f.message, ...('unlockUrl' in f && f.unlockUrl ? { unlockUrl: f.unlockUrl } : {}) } });
      await this.spawnServe(root, port, g);
      return;
    }
    this.set({ phase: 'down', owned: false, pid: null, problem: { code: f.code, message: f.message } });
  }

  private scheduleRespawn(root: string, port: number, g: number): void {
    const now = Date.now();
    this.respawns = this.respawns.filter((t) => now - t < 5 * 60000);
    if (this.respawns.length >= RESPAWN.length) {
      this.set({ phase: 'down', owned: false, problem: { code: 'crashed', message: 'harness serve keeps exiting: start it again by hand' } });
      return;
    }
    const delay = RESPAWN[this.respawns.length];
    this.respawns.push(now);
    this.respawnTimer = setTimeout(async () => {
      this.respawnTimer = null;
      if (g !== this.gen) return;
      const h = await healthz(this.st.url!);           // a USR1 re-exec shows up as a new pid: just resume
      if (h) { await this.attach(this.st.url!, true, g); return; }
      await this.spawnServe(root, port, g);
    }, delay);
  }

  async action(a: BackendAction): Promise<BackendState> {
    const root = this.st.root;
    switch (a.action) {
      case 'start': case 'retry': this.respawns = []; await this.start(); break;
      case 'restart':
        await this.stopServe(true);
        this.respawns = [];
        await this.fresh();
        break;
      case 'stop': await this.stopServe(true); this.set({ phase: 'down', problem: { code: 'stopped', message: 'backend stopped' } }); break;
      case 'set-root':
        if (!validRoot(a.root)) { this.set({ problem: { code: 'bad-root', message: `${a.root} is not a harness install` } }); break; }
        await this.stopServe(true);              // the old install's serve must not keep running under the new root
        this.d.patchSettings({ backend: { installRoot: a.root } });
        await this.fresh();
        break;
      case 'create-env':
        if (root && !existsSync(join(root, '.env')) && existsSync(join(root, '.env.example'))) {
          copyFileSync(join(root, '.env.example'), join(root, '.env'));
          try { chmodSync(join(root, '.env'), 0o600); } catch { /* best effort */ }
        }
        await this.fresh();
        break;
      case 'consent-host':
        if (this.d.confirmHost && !(await this.d.confirmHost())) break;
        this.d.patchSettings({ backend: { hostConsent: true } }); this.set({}); break;
      case 'login': {
        if (!this.client) break;
        const ok = await this.client.login(a.code).catch(() => false);
        if (ok) { this.d.saveAccessCode(a.code); await this.attach(this.client.base, this.st.owned, this.gen); }
        else this.set({ phase: 'needs-login', problem: { code: 'auth', message: 'that access code was not accepted' } });
        break;
      }
      case 'create-code': {
        if (!root || !this.client) { this.set({ problem: { code: 'no-install', message: 'no harness install to create a code with' } }); break; }
        const r = await runHarness(root, ['serve', 'code', 'add', `desktop-${hostname().replace(/[^A-Za-z0-9._-]/g, '-')}`], { timeoutMs: 30000 });
        const code = r.stdout.trim().split('\n').pop()?.trim() || '';
        if (r.code !== 0 || !code) { this.set({ problem: { code: 'create-code', message: (r.stderr || 'could not create a code').trim().slice(-300) } }); break; }
        const ok = await this.client.login(code).catch(() => false);
        if (ok) { this.d.saveAccessCode(code); await this.attach(this.client.base, this.st.owned, this.gen); }
        else this.set({ phase: 'needs-login', problem: { code: 'auth', message: 'the new code was not accepted' } });
        break;
      }
    }
    return this.get();
  }

  /** Stop an owned serve: SIGTERM every spawned serve's process group first, then `harness serve stop` (capped,
   * L1) for a serve that re-exec'd or was respawned. An attached serve (not ours) is never stopped. */
  async stopServe(_force: boolean): Promise<void> {
    this.gen++;
    this.stopping = true;
    this.poller.stop();
    if (this.respawnTimer) { clearTimeout(this.respawnTimer); this.respawnTimer = null; }
    if (this.reprobeTimer) { clearTimeout(this.reprobeTimer); this.reprobeTimer = null; }
    const kids = [...this.children];
    if (!this.st.owned && !kids.length) return;
    for (const c of kids) if (c.pid) { try { process.kill(-c.pid, 'SIGTERM'); } catch { /* gone */ } }
    this.child = null;
    if (this.st.root) await runHarness(this.st.root, ['serve', 'stop'], { timeoutMs: STOP_CLI_MS }).catch(() => null);
    this.set({ owned: false, pid: null });
  }

  /** Spawned serves still alive (tests, diagnostics). */
  spawned(): number[] { return [...this.children].map((c) => c.pid ?? 0); }

  busy(): boolean {
    const r = this.lastQueue?.runner;
    return !!(r && (r.running || r.starting));
  }

  /** Quit policy (1.2 step 9). */
  async onQuit(): Promise<void> {
    this.poller.stop();
    if (!this.st.owned && !this.children.size) return;
    const pol = this.d.settings().backend.onQuit;
    if (pol === 'keep' || (pol === 'keep-if-busy' && this.busy())) {
      this.gen++; this.stopping = true;            // the serve outlives us: no respawn / reprobe from here on
      if (this.respawnTimer) { clearTimeout(this.respawnTimer); this.respawnTimer = null; }
      if (this.reprobeTimer) { clearTimeout(this.reprobeTimer); this.reprobeTimer = null; }
      for (const c of this.children) c.unref();
      return;
    }
    await this.stopServe(true);
  }
}

export async function healthz(url: string): Promise<{ state: string; pid: number; stale: boolean } | null> {
  const c = new ServeClient(url);
  try {
    const r = await c.raw('/healthz', { timeoutMs: 2000 });
    if (r.status !== 200) return null;
    return { state: String(r.headers['x-harness-state'] || ''), pid: Number(r.headers['x-harness-pid'] || 0), stale: r.headers['x-harness-stale'] === '1' };
  } catch { return null; }
}

/** First port in [from, from+n) whose /healthz reports `state` (our install), probed in parallel. */
async function findServe(from: number, n: number, state: string): Promise<number | null> {
  const ports = Array.from({ length: n }, (_, i) => from + i).filter((p) => p < 65536);
  const hs = await Promise.all(ports.map((p) => healthz(`http://127.0.0.1:${p}`)));
  const i = hs.findIndex((h) => h && h.state === state);
  return i < 0 ? null : ports[i];
}

async function findFreePort(from: number): Promise<number> {
  const net = await import('node:net');
  for (let p = from; p < from + 100 && p < 65535; p++) {
    const ok = await new Promise<boolean>((res) => {
      const s = net.createServer();
      s.once('error', () => res(false));
      s.listen(p, '127.0.0.1', () => s.close(() => res(true)));
    });
    if (ok) return p;
  }
  return from;
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
