// Pure terminal helpers (unit-tested): titles, paste confirmation, WebGL slot cap, exit classification,
// the pty-event dispatcher's buffer for data that arrives before spawn() resolves.
import type { PtySpec } from '../../../shared/ipc';

export const KIND_LABEL: Record<PtySpec['kind'], string> = { agent: 'agent', host: 'host agent', shell: 'shell', plain: 'plain shell' };
export const KIND_GLYPH: Record<PtySpec['kind'], string> = { agent: '◆', host: '◇', shell: '▣', plain: '›' };

export function baseName(p: string): string {
  const s = p.replace(/[\\/]+$/, '');
  const i = Math.max(s.lastIndexOf('/'), s.lastIndexOf('\\'));
  return (i >= 0 ? s.slice(i + 1) : s) || p || '/';
}

/** Tab label = folder name + mode glyph (W3). */
export function specTitle(spec: PtySpec): string {
  const t = spec.ticket ? ` ${spec.ticket}` : '';
  return `${KIND_GLYPH[spec.kind] ?? ''} ${baseName(spec.cwd)}${t}`.trim();
}

/** Number of lines a paste would send. */
export function pasteLines(text: string): number {
  if (!text) return 0;
  return text.replace(/\r\n/g, '\n').replace(/\n$/, '').split('\n').length;
}
/** 3.7: a paste of more than 5 lines or containing a newline asks once (unless disabled or remembered). */
export function pasteNeedsConfirm(text: string, setting: boolean, remembered: boolean): boolean {
  if (!setting || remembered || !text) return false;
  return /[\r\n]/.test(text) || pasteLines(text) > 5;
}

/** WebGL contexts are capped (DR(g)): at most `cap` terminals hold one; others use the DOM renderer. */
export class WebglSlots {
  private held = new Set<string>();
  constructor(public cap = 8) {}
  acquire(id: string): boolean {
    if (this.held.has(id)) return true;
    if (this.held.size >= this.cap) return false;
    this.held.add(id);
    return true;
  }
  release(id: string): void { this.held.delete(id); }
  get size(): number { return this.held.size; }
}

export interface ExitInfo { code: number; signal: number }
/** W2 step 4: a non-zero exit within 5 s of start counts as a launch failure (offer Edit options / Try host mode). */
export function isLaunchFailure(startedAt: number, exitedAt: number, e: ExitInfo): boolean {
  return (e.code !== 0 || e.signal !== 0) && exitedAt - startedAt < 5000;
}
export function exitText(e: ExitInfo): string {
  if (e.signal) return `Process exited (signal ${e.signal})`;
  return `Process exited (code ${e.code})`;
}
/** Kinds that run in the container and can retry on the host. */
export function canTryHost(spec: PtySpec): boolean { return spec.kind === 'agent'; }

/** Buffers pty output for ids nobody has claimed yet (data racing the spawn() reply). Bounded per id. */
export class PendingBuffer {
  private map = new Map<string, { chunks: string[]; size: number; at: number }>();
  constructor(private capChars = 256 * 1024, private ttlMs = 15000) {}
  push(id: string, data: string, now: number): void {
    this.gc(now);
    const e = this.map.get(id) ?? { chunks: [], size: 0, at: now };
    if (e.size + data.length > this.capChars) return;
    e.chunks.push(data); e.size += data.length;
    this.map.set(id, e);
  }
  take(id: string): string[] { const e = this.map.get(id); this.map.delete(id); return e ? e.chunks : []; }
  gc(now: number): void { for (const [k, v] of this.map) if (now - v.at > this.ttlMs) this.map.delete(k); }
  get ids(): string[] { return [...this.map.keys()]; }
}
