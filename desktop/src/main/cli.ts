// CLI runner: runs `bash <root>/harness ...` with argv arrays (never a shell string), captures or streams output.
import { spawn, type ChildProcess } from 'node:child_process';
import type { ConfigRow } from '../shared/api';
import type { HarnessUpdateState, JobInfo } from '../shared/ipc';

export interface RunResult { code: number; stdout: string; stderr: string; timedOut: boolean }

export function cliEnv(extra: Record<string, string> = {}): NodeJS.ProcessEnv {
  return { ...process.env, NO_COLOR: '1', TERM: 'dumb', HARNESS_OPEN: '0', ...extra };
}

/** Signal a detached child's whole process group (falls back to the child alone). */
export function killGroup(child: ChildProcess, sig: NodeJS.Signals): void {
  try { if (child.pid) { process.kill(-child.pid, sig); return; } } catch { /* not a group leader / gone */ }
  try { child.kill(sig); } catch { /* gone */ }
}

/** Run `bash <root>/harness args`. The child leads its own process group so a timeout kills grandchildren too,
 * and the promise resolves on timeout even when a grandchild keeps the pipes open (L9). */
export function runHarness(root: string, args: string[], o: { timeoutMs?: number; env?: Record<string, string>; cwd?: string; killGraceMs?: number } = {}): Promise<RunResult> {
  return new Promise((resolve) => {
    let out = ''; let err = ''; let timedOut = false; let done = false;
    let child: ChildProcess;
    try {
      child = spawn('bash', [`${root}/harness`, ...args], { cwd: o.cwd || root, env: cliEnv(o.env), stdio: ['ignore', 'pipe', 'pipe'], detached: true });
    } catch (e) { resolve({ code: 127, stdout: '', stderr: String(e), timedOut: false }); return; }
    const finish = (code: number) => { if (done) return; done = true; clearTimeout(t); resolve({ code, stdout: out, stderr: err, timedOut }); };
    const cap = (s: string, c: Buffer) => (s.length > 2_000_000 ? s : s + c.toString('utf8'));
    child.stdout!.on('data', (c: Buffer) => { out = cap(out, c); });
    child.stderr!.on('data', (c: Buffer) => { err = cap(err, c); });
    const grace = o.killGraceMs ?? 2000;
    const t = setTimeout(() => {
      timedOut = true;
      killGroup(child, 'SIGTERM');
      setTimeout(() => { killGroup(child, 'SIGKILL'); setTimeout(() => finish(124), 200).unref(); }, grace).unref();
    }, o.timeoutMs ?? 60000);
    child.on('error', (e) => { err += String(e); });
    child.on('close', (code, sig) => finish(code ?? (sig ? 128 : 1)));
  });
}

export type JobKind = JobInfo['job'];
const JOB_TAIL = 64 * 1024;
const JOB_RING = 8;
/** J1: a job that runs longer than this is stopped (git waiting on the network or a credential prompt). */
export const JOB_TIMEOUT_MS: Record<JobKind, number> = { doctor: 5 * 60000, 'check-updates': 2 * 60000, upgrade: 15 * 60000, 'serve-stop': 60000 };
/** Something that can stop a started job (startJob returns the child). */
export interface JobHandle { kill(sig?: NodeJS.Signals): unknown }

/** Harness CLI jobs (M3): one running job per kind (a second request gets the running job's id back), and a
 * small ring of recent jobs with an output tail so a reopened Updates panel can re-attach. */
export class Jobs {
  private seq = 0;
  private ring: JobInfo[] = [];
  private stops = new Map<string, (why: string) => void>();   // running job id -> stop it (J1)
  constructor(private d: {
    start(root: string, args: string[], onChunk: (s: string) => void, onExit: (code: number) => void): JobHandle | null | void;
    emit(ch: 'job:output' | 'job:exit', p: unknown): void;
    done?(job: JobKind, output: string, code: number): void;
    timeoutMs?: Partial<Record<JobKind, number>>;
    /** After a stop, how long to wait for the process to exit before the job is reported ended anyway. */
    graceMs?: number;
  }) {}

  static args(job: JobKind): string[] {
    return { doctor: ['doctor'], 'check-updates': ['check-updates'], upgrade: ['upgrade', '--no-prompt'], 'serve-stop': ['serve', 'stop'] }[job];
  }

  run(job: JobKind, root: string | null): { id: string } {
    const live = this.ring.find((j) => j.running && j.job === job);
    if (live) return { id: live.id };
    // J1: a check while an upgrade runs would contend on the clone's git lock and read a half-updated tree
    const blocked = job === 'check-updates' && this.ring.some((j) => j.running && j.job === 'upgrade');
    const info: JobInfo = { id: `job-${++this.seq}`, job, running: true, code: null, startedAt: Date.now(), output: '' };
    this.ring.push(info);
    while (this.ring.length > JOB_RING) { const i = this.ring.findIndex((j) => !j.running); if (i < 0) break; this.ring.splice(i, 1); }
    let all = '';
    const chunk = (c: string) => {
      if (all.length < 2_000_000) all += c;
      info.output = (info.output + c).slice(-JOB_TAIL);
      this.d.emit('job:output', { id: info.id, chunk: c });
    };
    let timer: NodeJS.Timeout | null = null;
    let force: NodeJS.Timeout | null = null;
    const exit = (code: number) => {
      if (!info.running) return;                  // once: the process may still close after a forced end
      info.running = false; info.code = code;
      if (timer) clearTimeout(timer);
      if (force) clearTimeout(force);
      this.stops.delete(info.id);
      try { this.d.done?.(job, all, code); } finally { this.d.emit('job:exit', { id: info.id, code }); }
    };
    // async so the caller gets the id before any output event
    setTimeout(() => {
      if (!root) { chunk('no harness install\n'); exit(127); return; }
      if (blocked) { chunk('an upgrade is running: check for updates again when it has finished\n'); exit(1); return; }
      const h = this.d.start(root, Jobs.args(job), chunk, exit) || null;
      if (!info.running) return;
      const stop = (why: string) => {
        if (!info.running || force) return;
        chunk(`\n[desktop] ${why}: stopping ${job}\n`);
        try { h?.kill('SIGTERM'); } catch { /* gone */ }
        force = setTimeout(() => { try { h?.kill('SIGKILL'); } catch { /* gone */ } exit(124); }, this.d.graceMs ?? 3000);
        force.unref?.();
      };
      this.stops.set(info.id, stop);
      const ms = this.d.timeoutMs?.[job] ?? JOB_TIMEOUT_MS[job];
      timer = setTimeout(() => stop(`no end after ${Math.round(ms / 1000)} s`), ms);
      timer.unref?.();
    }, 0);
    return { id: info.id };
  }

  list(): JobInfo[] { return this.ring.map((j) => ({ ...j })); }
  /** J1: stop every running job (app quit); resolves once each has ended or was given up on. */
  async stopAll(why = 'the app is quitting'): Promise<void> {
    const ids = [...this.stops.keys()];
    for (const id of ids) this.stops.get(id)?.(why);
    const t0 = Date.now();
    while (this.ring.some((j) => j.running && ids.includes(j.id)) && Date.now() - t0 < (this.d.graceMs ?? 3000) + 500) await new Promise((r) => setTimeout(r, 50));
  }
  running(): JobKind[] { return this.ring.filter((j) => j.running).map((j) => j.job); }
}

/** Streams a job's output; returns a handle whose kill() signals the job's whole process group (J1: git and
 * friends it started go too). */
export function startJob(root: string, args: string[], onChunk: (s: string) => void, onExit: (code: number) => void,
  env: Record<string, string> = {}): JobHandle | null {
  try {
    const child = spawn('bash', [`${root}/harness`, ...args], { cwd: root, env: cliEnv(env), stdio: ['ignore', 'pipe', 'pipe'], detached: true });
    const f = framer(onChunk);
    child.stdout!.on('data', f); child.stderr!.on('data', f);
    child.on('error', (e) => onChunk(`${e}\n`));
    child.on('close', (code) => { f.flush(); onExit(code ?? 1); });
    return { kill: (sig: NodeJS.Signals = 'SIGTERM') => killGroup(child, sig) };
  } catch (e) { onChunk(String(e)); onExit(127); return null; }
}

/** Job output framing: decode UTF-8 safely across chunk edges, emit at most every 50 ms. */
export function framer(emit: (s: string) => void, everyMs = 50): ((c: Buffer) => void) & { flush(): void } {
  const dec = new TextDecoder('utf-8');
  let buf = ''; let timer: NodeJS.Timeout | null = null;
  const flush = () => { if (timer) { clearTimeout(timer); timer = null; } const tail = dec.decode(); if (tail) buf += tail; if (buf) { emit(buf); buf = ''; } };
  const fn = ((c: Buffer) => {
    buf += dec.decode(c, { stream: true });
    if (!timer) timer = setTimeout(() => { timer = null; if (buf) { emit(buf); buf = ''; } }, everyMs);
  }) as ((c: Buffer) => void) & { flush(): void };
  fn.flush = flush;
  return fn;
}

/** `harness config list --tsv`: KEY \t value \t secret(1/0) \t description. */
export function parseConfigTsv(text: string): ConfigRow[] {
  const rows: ConfigRow[] = [];
  for (const line of text.split('\n')) {
    if (!line.trim()) continue;
    const parts = line.replace(/\r$/, '').split('\t');
    if (parts.length < 3 || !/^[A-Z][A-Z0-9_]*$/.test(parts[0])) continue;
    rows.push({ key: parts[0], value: parts[1] ?? '', secret: parts[2] === '1', desc: parts.slice(3).join('\t'), group: groupOf(parts[0]) });
  }
  return rows;
}

function groupOf(key: string): string {
  if (/^(PROXY|OAI|DEFAULT_MODEL|CHATGPT)/.test(key)) return 'model';
  if (/^(TRACKER|JIRA)/.test(key)) return 'tracker';
  if (/^QUEUE/.test(key)) return 'queue';
  return 'general';
}

/** Chars of a masked secret value "(set, N chars)"; 0 when unset. */
export function maskedChars(value: string): number {
  const m = /^\(set, (\d+) chars\)$/.exec(value.trim());
  return m ? Number(m[1]) : value ? value.length : 0;
}

/** Required .env values for the openai backend, same rule as the CLI's _openai_backend_missing. */
export function missingRequired(rows: ConfigRow[]): string[] {
  const val = (k: string) => rows.find((r) => r.key === k)?.value ?? '';
  const has = (k: string) => val(k) !== '' && val(k) !== '(not set)';
  const req: string[] = [];
  const anyOai = has('OAI_API_URL') || has('OAI_API_KEY') || has('OAI_MODELS');
  if (anyOai) req.push('OAI_API_URL', 'OAI_API_KEY', 'OAI_MODELS');
  if (has('PROXY_API_URL') || has('PROXY_API_KEY') || !anyOai) {
    req.push('PROXY_API_URL', 'PROXY_API_KEY');
    if (!anyOai) req.push('DEFAULT_MODEL_NAME');
  }
  // Only keys the listing knows about can be judged; unknown keys are not reported as missing.
  return req.filter((k) => rows.some((r) => r.key === k) && !has(k));
}

/** `harness check-updates` output -> state. Matches the CLI's wording loosely; unknown text keeps available=null. */
export function parseCheckUpdates(text: string, code: number, now: number): HarnessUpdateState {
  const t = text.replace(/\x1b\[[0-9;]*m/g, '');
  // Wording of cmd_check_updates: "up to date with origin/<branch> (<sha7>)" / "update available on <branch>; run 'harness upgrade'".
  const branch = /up to date with origin\/([\w./-]+)/.exec(t)?.[1] ?? /update available on ([\w./-]+?);/.exec(t)?.[1] ?? '';
  const local = /origin\/[\w./-]+ \(([0-9a-f]{7,40})\)/.exec(t)?.[1] ?? '';
  let available: boolean | null = null;
  if (/up to date with origin\//.test(t)) available = false;
  else if (/update available on /.test(t)) available = true;
  return { branch, local, available, text: t.trim().slice(-4000), checkedAt: now };
}
