// pty-host: utilityProcess entry. Owns every node-pty; coalesces output (8 ms) and applies flow control
// (pause above 1 MiB unacked, resume below 256 KiB).
import * as pty from 'node-pty';

type In =
  | { t: 'spawn'; id: string; file: string; args: string[]; cwd: string; env: Record<string, string>; cols: number; rows: number }
  | { t: 'write'; id: string; data: string } | { t: 'resize'; id: string; cols: number; rows: number }
  | { t: 'ack'; id: string; chars: number } | { t: 'kill'; id: string } | { t: 'ping'; n: number } | { t: 'killall' };

const HIGH = 1024 * 1024; const LOW = 256 * 1024; const COALESCE_MS = 8;
interface P { p: pty.IPty; buf: string; timer: NodeJS.Timeout | null; unacked: number; paused: boolean }
const ptys = new Map<string, P>();
const port = (process as unknown as { parentPort: { on(e: 'message', cb: (m: { data: In }) => void): void; postMessage(m: unknown): void } }).parentPort;
const post = (m: unknown) => port.postMessage(m);

function flush(id: string): void {
  const s = ptys.get(id);
  if (!s) return;
  s.timer = null;
  if (!s.buf) return;
  const data = s.buf; s.buf = '';
  s.unacked += data.length;
  post({ t: 'data', id, data });
  if (!s.paused && s.unacked > HIGH) { s.paused = true; try { s.p.pause(); } catch { /* exited */ } }
}

port.on('message', ({ data: m }) => {
  try {
    if (m.t === 'spawn') {
      const env: Record<string, string> = {};
      for (const [k, v] of Object.entries(process.env)) if (typeof v === 'string') env[k] = v;
      Object.assign(env, m.env);
      const p = pty.spawn(m.file, m.args, { name: 'xterm-256color', cols: m.cols, rows: m.rows, cwd: m.cwd, env });
      const s: P = { p, buf: '', timer: null, unacked: 0, paused: false };
      ptys.set(m.id, s);
      p.onData((d) => { s.buf += d; if (!s.timer) s.timer = setTimeout(() => flush(m.id), COALESCE_MS); });
      p.onExit(({ exitCode, signal }) => {
        flush(m.id);
        ptys.delete(m.id);
        post({ t: 'exit', id: m.id, code: exitCode, signal: signal ?? 0 });
      });
      post({ t: 'spawned', id: m.id, pid: p.pid });
    } else if (m.t === 'write') ptys.get(m.id)?.p.write(m.data);
    else if (m.t === 'resize') { const s = ptys.get(m.id); if (s) s.p.resize(Math.max(2, m.cols), Math.max(1, m.rows)); }
    else if (m.t === 'ack') {
      const s = ptys.get(m.id);
      if (s) { s.unacked = Math.max(0, s.unacked - m.chars); if (s.paused && s.unacked < LOW) { s.paused = false; s.p.resume(); } }
    } else if (m.t === 'kill') {
      // SIGHUP first; a process that ignores it (or sits paused by flow control) gets SIGKILL after 3 s
      const s = ptys.get(m.id);
      if (s) {
        try { s.p.kill(); } catch { /* gone */ }
        setTimeout(() => { if (ptys.get(m.id) === s) { try { s.p.kill('SIGKILL'); } catch { /* gone */ } } }, 3000).unref();
      }
    }
    else if (m.t === 'killall') { for (const s of ptys.values()) { try { s.p.kill(); } catch { /* gone */ } } }
    else if (m.t === 'ping') post({ t: 'pong', n: m.n, mem: process.memoryUsage().rss, unacked: [...ptys.values()].reduce((a, s) => a + s.unacked, 0) });
  } catch (e) {
    if ('id' in m) post({ t: 'error', id: m.id, message: String((e as Error)?.message || e) });
  }
});
post({ t: 'ready' });
