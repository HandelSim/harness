// Backend manager (1.2): locate -> config check -> attach or spawn -> up, with reconnect, respawn limits,
// stale detection, access-code login and quit policy.
import { spawn, type ChildProcess } from 'node:child_process';
import { appendFileSync, copyFileSync, existsSync, mkdirSync } from 'node:fs';
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
}

const RING = 400;
const READY_MS = 120000;
const RESPAWN = [1000, 5000, 15000];

export class BackendManager {
  st: BackendState = { phase: 'locating', root: null, url: null, port: null, owned: false, pid: null, stale: false,
    apiVersion: null, missing: [], hostConsent: false, problem: null, logTail: [], since: Date.now() };
  client: ServeClient | null = null;
  status: Status | null = null;
  poller: Poller;
  private child: ChildProcess | null = null;
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

  async start(): Promise<void> {
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
      if (s.ok && s.data?.mode === 'serve') { await this.attach(url, false, g); return; }
      if (!s.ok && s.status === 404) {
        this.set({ phase: 'incompatible', url, port, problem: { code: 'old-serve', message: 'the running harness serve predates the app API: restart the backend' } });
        return;
      }
      if (!s.ok && s.error.code === 'auth') { await this.attach(url, false, g); return; }
      // viewer (or anything else of ours): harness serve takes the port over by itself
      await this.spawnServe(root, port, g);
      return;
    }
    if (h) {
      // another install owns the port: next free one from port+10
      const free = await findFreePort(port + 10);
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

  private async spawnServe(root: string, port: number, g: number): Promise<void> {
    if (this.child) return;
    this.set({ phase: 'starting', owned: true, port, url: `http://127.0.0.1:${port}`, logTail: [] });
    const extra: Record<string, string> = { HARNESS_OPEN: '0' };
    if (this.d.settings().backend.hostConsent) extra.HARNESS_HOST_CONFIRM = '1';
    if (this.skipProbe) extra.HARNESS_SKIP_AUTH_PROBE = '1';
    const cwd = this.d.lastProject() || this.d.home;
    let child: ChildProcess;
    try {
      child = spawn('bash', [join(root, 'harness'), 'serve', '--port', String(port)], {
        cwd: existsSync(cwd) ? cwd : this.d.home, env: cliEnv(extra), stdio: ['ignore', 'pipe', 'pipe'], detached: true });
    } catch (e) { this.set({ phase: 'down', owned: false, problem: { code: 'spawn', message: String(e) } }); return; }
    this.child = child;
    this.set({ pid: child.pid ?? null });
    mkdirSync(this.d.logDir, { recursive: true });
    const logFile = join(this.d.logDir, 'serve.log');
    let all = '';
    let partial = '';
    const onData = (c: Buffer) => {
      const s = c.toString('utf8');
      try { appendFileSync(logFile, s, { mode: 0o600 }); } catch { /* ignore */ }
      if (all.length < 200000) all += s;
      const lines = (partial + s).split('\n');
      partial = lines.pop() || '';
      if (lines.length) {
        const tail = [...this.st.logTail, ...lines.map((l) => l.replace(/\x1b\[[0-9;?]*[A-Za-z]/g, ''))].slice(-RING);
        this.st = { ...this.st, logTail: tail };
        if (this.st.phase === 'starting') this.d.emit(this.get());
      }
    };
    child.stdout!.on('data', onData); child.stderr!.on('data', onData);
    let exited = false;
    child.on('exit', (code) => {
      exited = true;
      this.child = null;
      if (this.stopping || g !== this.gen) return;
      log('warn', `owned serve exited with ${code}`);
      if (this.st.phase === 'starting') { void this.failed(root, port, all, g); return; }
      this.set({ phase: 'reconnecting', pid: null });
      this.scheduleRespawn(root, port, g);
    });
    const t0 = Date.now();
    while (!exited && Date.now() - t0 < READY_MS && g === this.gen) {
      await sleep(250);
      const h = await healthz(this.st.url!);
      if (h && !exited) { if (await this.attach(this.st.url!, true, g)) return; if (this.st.phase !== 'starting' && this.st.phase !== 'down') return; }
    }
    if (!exited && g === this.gen && this.st.phase === 'starting') {
      this.set({ phase: 'down', problem: { code: 'timeout', message: 'harness serve did not come up in 120 s' } });
    }
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
        await this.start();
        break;
      case 'stop': await this.stopServe(true); this.set({ phase: 'down', problem: { code: 'stopped', message: 'backend stopped' } }); break;
      case 'set-root':
        if (!validRoot(a.root)) { this.set({ problem: { code: 'bad-root', message: `${a.root} is not a harness install` } }); break; }
        this.d.patchSettings({ backend: { installRoot: a.root } });
        await this.start();
        break;
      case 'create-env':
        if (root && !existsSync(join(root, '.env')) && existsSync(join(root, '.env.example'))) {
          copyFileSync(join(root, '.env.example'), join(root, '.env'));
        }
        await this.start();
        break;
      case 'consent-host': this.d.patchSettings({ backend: { hostConsent: true } }); this.set({}); break;
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

  /** Stop an owned serve (harness serve stop, then the process group). An attached serve is never stopped. */
  async stopServe(force: boolean): Promise<void> {
    this.gen++;
    this.stopping = true;
    this.poller.stop();
    if (this.respawnTimer) { clearTimeout(this.respawnTimer); this.respawnTimer = null; }
    if (!this.st.owned && !force) return;
    if (!this.st.owned) return;
    if (this.st.root) await runHarness(this.st.root, ['serve', 'stop'], { timeoutMs: 20000 }).catch(() => null);
    const c = this.child;
    if (c && c.pid) { try { process.kill(-c.pid, 'SIGTERM'); } catch { /* gone */ } }
    this.child = null;
    this.set({ owned: false, pid: null });
  }

  busy(): boolean {
    const r = this.lastQueue?.runner;
    return !!(r && (r.running || r.starting));
  }

  /** Quit policy (1.2 step 9). */
  async onQuit(): Promise<void> {
    this.poller.stop();
    if (!this.st.owned) return;
    const pol = this.d.settings().backend.onQuit;
    if (pol === 'keep' || (pol === 'keep-if-busy' && this.busy())) { this.child?.unref(); return; }
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
