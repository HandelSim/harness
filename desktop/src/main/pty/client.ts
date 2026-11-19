// Main-side PTY manager: forks the pty-host utilityProcess, relays data, keeps a 256 KiB replay ring per
// terminal for reattach after a renderer reload, marks tabs ended when the host dies. A terminal's record is
// dropped on pty:kill and EXIT_KEEP_MS after it exits, so pty:list only lists terminals that can still matter.
import * as electron from 'electron';
import type { PtyInfo } from '../../shared/ipc';
import { buildArgv, cleanSpec } from './spec';
import { log } from '../log';
import type { handle as Handle, listen as Listen, v as V } from '../ipc';
import { bashPath } from '../bash';

const REPLAY = 256 * 1024;
export const EXIT_KEEP_MS = 60000;            // an exited terminal stays attachable this long (renderer reload)
/** Replay ring as a chunk list with a running length (L4): appending is O(chunk), flattened only on attach. */
interface Term { info: PtyInfo; chunks: string[]; len: number; reap: NodeJS.Timeout | null }
interface HostProc {
  pid?: number; postMessage(m: unknown): void; kill(): boolean;
  on(e: 'message', cb: (m: never) => void): unknown; on(e: 'exit', cb: (code: number) => void): unknown;
}
type Fork = (path: string) => HostProc;
const electronFork: Fork = (path) =>
  electron.utilityProcess.fork(path, [], { serviceName: 'harness-pty-host', stdio: 'ignore' }) as unknown as HostProc;

export function pushReplay(t: Pick<Term, 'chunks' | 'len'>, data: string, cap = REPLAY): void {
  t.chunks.push(data); t.len += data.length;
  while (t.chunks.length > 1 && t.len - t.chunks[0].length >= cap) t.len -= t.chunks.shift()!.length;
}
export function flatReplay(t: Pick<Term, 'chunks' | 'len'>, cap = REPLAY): string {
  const s = t.chunks.length === 1 ? t.chunks[0] : t.chunks.join('');
  if (t.chunks.length > 1) { t.chunks = [s]; }
  return s.length > cap ? s.slice(-cap) : s;
}

export class PtyClient {
  private proc: HostProc | null = null;
  private terms = new Map<string, Term>();
  private seq = 0;
  private waits = new Map<string, (r: { ok: true; pid: number } | { ok: false; message: string }) => void>();
  private pings = new Map<number, (r: { mem: number; unacked: number }) => void>();
  private pingSeq = 0;
  constructor(private d: { hostPath: string; root: () => string | null; hostConsent: () => boolean; emit: (ch: string, p: unknown) => void;
    fork?: Fork; exitKeepMs?: number }) {}

  private host(): HostProc {
    if (this.proc) return this.proc;
    const p = (this.d.fork ?? electronFork)(this.d.hostPath);
    p.on('message', (m: { t: string; id: string; data?: string; code?: number; signal?: number; pid?: number; message?: string; n?: number; mem?: number; unacked?: number }) => {
      if (m.t === 'data' && m.data !== undefined) {
        const t = this.terms.get(m.id);
        if (t) pushReplay(t, m.data);
        this.d.emit('pty:data', { id: m.id, data: m.data });
      } else if (m.t === 'exit') {
        const t = this.terms.get(m.id);
        if (t) { t.info.exited = { code: m.code ?? 0, signal: m.signal ?? 0 }; this.reapLater(m.id, t); }
        this.d.emit('pty:exit', { id: m.id, code: m.code ?? 0, signal: m.signal ?? 0 });
      } else if (m.t === 'spawned') {
        const w = this.waits.get(m.id);
        // P1: an answer after the 10 s wait gave up: nobody holds that pty, so it could never be killed; kill it now
        if (w) { w({ ok: true, pid: m.pid ?? 0 }); this.waits.delete(m.id); } else if (!this.terms.has(m.id)) p.postMessage({ t: 'kill', id: m.id });
      }
      else if (m.t === 'error') { const w = this.waits.get(m.id); if (w) { w({ ok: false, message: m.message || 'spawn failed' }); this.waits.delete(m.id); } }
      else if (m.t === 'pong') { this.pings.get(m.n!)?.({ mem: m.mem ?? 0, unacked: m.unacked ?? 0 }); this.pings.delete(m.n!); }
    });
    p.on('exit', (code) => {
      log('warn', `pty-host exited with ${code}`);
      this.proc = null;
      for (const [id, t] of this.terms) if (!t.info.exited) { t.info.exited = { code: -1, signal: 9 }; this.reapLater(id, t); this.d.emit('pty:exit', { id, code: -1, signal: 9 }); }
      for (const w of this.waits.values()) w({ ok: false, message: 'pty-host exited' });
      this.waits.clear();
    });
    this.proc = p;
    return p;
  }

  private reapLater(id: string, t: Term): void {
    if (t.reap) return;
    t.reap = setTimeout(() => { if (this.terms.get(id) === t) this.terms.delete(id); }, this.d.exitKeepMs ?? EXIT_KEEP_MS);
    t.reap.unref?.();
  }

  /** pty:kill: ask the host to kill it and forget the record now (the host's later 'exit' still reaches the renderer). */
  kill(id: string): void {
    const t = this.terms.get(id);
    if (!t) return;
    if (this.proc) this.proc.postMessage({ t: 'kill', id });
    if (t.reap) clearTimeout(t.reap);
    this.terms.delete(id);
  }

  /** Terminals still running (app:busy, the W15 confirm). */
  liveCount(): number { let n = 0; for (const t of this.terms.values()) if (!t.info.exited) n++; return n; }

  pid(): number | undefined { return this.proc?.pid; }
  /** e2e/diagnostics: host memory and total unacked chars. */
  stats(): Promise<{ mem: number; unacked: number }> {
    const n = ++this.pingSeq;
    return new Promise((res) => { this.pings.set(n, res); this.host().postMessage({ t: 'ping', n }); setTimeout(() => { if (this.pings.delete(n)) res({ mem: -1, unacked: -1 }); }, 3000); });
  }
  killHost(): void { this.proc?.kill(); }

  async spawn(rawSpec: unknown, cols: number, rows: number) {
    const spec = cleanSpec(rawSpec);
    if (!spec) return { ok: false as const, error: { code: 'bad-spec', message: 'invalid terminal spec' } };
    const r = buildArgv(spec, { root: this.d.root(), shell: process.env.SHELL, bash: bashPath(), hostConsent: this.d.hostConsent() });
    if (!r.ok) return r;
    const id = `t${Date.now().toString(36)}${(++this.seq).toString(36)}`;
    const res = await new Promise<{ ok: true; pid: number } | { ok: false; message: string }>((resolve) => {
      this.waits.set(id, resolve);
      this.host().postMessage({ t: 'spawn', id, ...r.argv, cols, rows });
      setTimeout(() => { if (this.waits.delete(id)) resolve({ ok: false, message: 'pty-host did not answer' }); }, 10000);
    });
    if (!res.ok) return { ok: false as const, error: { code: 'spawn', message: res.message } };
    this.terms.set(id, { info: { id, spec, pid: res.pid, startedAt: Date.now(), exited: null }, chunks: [], len: 0, reap: null });
    return { ok: true as const, id };
  }

  send(m: Record<string, unknown>): void { if (this.proc && this.terms.has(m.id as string)) this.proc.postMessage(m); }

  list(): PtyInfo[] { return [...this.terms.values()].map((t) => ({ ...t.info })); }
  attach(id: string): { info: PtyInfo; replay: string } | null { const t = this.terms.get(id); return t ? { info: { ...t.info }, replay: flatReplay(t) } : null; }

  async shutdown(): Promise<void> {
    if (!this.proc) return;
    this.proc.postMessage({ t: 'killall' });
    await new Promise((r) => setTimeout(r, 300));
    this.proc?.kill();
  }

  /** Packaged smoke: write a line, wait for `needle` in the output, kill the terminal. */
  async smokeEcho(id: string, line: string, needle: string, ms: number): Promise<boolean> {
    this.send({ t: 'write', id, data: line });
    const t0 = Date.now();
    while (Date.now() - t0 < ms) {
      const t = this.terms.get(id);
      if (t && flatReplay(t).includes(needle)) { this.send({ t: 'ack', id, chars: t.len }); this.kill(id); return true; }
      await new Promise((r) => setTimeout(r, 100));
    }
    this.kill(id);
    return false;
  }

  register(handle: typeof Handle, listen: typeof Listen, v: typeof V): void {
    handle('pty:spawn', (p) => {
      const o = v.obj(p);
      return { spec: o.spec, cols: v.int(o.cols, 'cols', 2, 1000), rows: v.int(o.rows, 'rows', 1, 500) };
    }, (p) => this.spawn(p.spec, p.cols, p.rows));
    const id = (o: Record<string, unknown>) => v.str(o.id, 'id', 64);
    listen('pty:write', (p) => { const o = v.obj(p); return { id: id(o), data: v.str(o.data, 'data', 1_000_000) }; }, (m) => this.send({ t: 'write', ...m }));
    listen('pty:resize', (p) => { const o = v.obj(p); return { id: id(o), cols: v.int(o.cols, 'cols', 2, 1000), rows: v.int(o.rows, 'rows', 1, 500) }; },
      (m) => this.send({ t: 'resize', ...m }));
    listen('pty:ack', (p) => { const o = v.obj(p); return { id: id(o), chars: v.int(o.chars, 'chars', 0, 1e9) }; }, (m) => this.send({ t: 'ack', ...m }));
    listen('pty:kill', (p) => ({ id: id(v.obj(p)) }), (m) => this.kill(m.id));
    handle('pty:list', v.none, () => this.list(), () => []);
    handle('pty:attach', (p) => id(v.obj(p)), (i) => this.attach(i), () => null);
  }
}
