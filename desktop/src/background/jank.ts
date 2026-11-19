// Main-thread jank feedback for the governor. The worker governor only sees its own CPU time per frame; on a
// software rasteriser (and for GPU/raster-bound effects) the cost lands on the compositor and shows up as late
// main-thread frames instead. JankMeter samples rAF intervals and long tasks on the main thread and reports a
// summary about once a second; isJanky() is the policy the worker governor applies (with its 2 s hold).

// The interval threshold is relative to the display's own frame time (vsyncMs: the lowest 10th-percentile interval
// seen since the meter was created, so a 30 Hz display or remote desktop is not "janky" at 33 ms). The worker rung
// gets workerJank(): long tasks are app work on the main thread, which a worker scene cannot cause, so they and the
// frames that overlap them do not count there.

export interface JankSummary { p95: number; longMs: number; windowMs: number; n: number;
  /** Display frame time estimate (ms); absent = assume 60 Hz. */ vsyncMs?: number;
  /** p95 of the intervals that do not overlap a long task. */ p95Free?: number }

export const JANK_P95_MS = 34;          // floor of the frame-interval threshold (two 60 Hz vsyncs)
export const JANK_LONG_FRAC = 0.25;     // or a quarter of the window spent in long tasks
export const JANK_MIN_SAMPLES = 20;
const VSYNC_MAX = 34.5;                 // ~29 Hz: no display is slower, so a slower estimate is the scene, not the display

/** Frame-interval threshold for a display frame time: two frames plus 4 ms of jitter, never below JANK_P95_MS.
 *  An estimate slower than any display (every frame slow from the first window) is ignored: 60 Hz rules then. */
export function jankThreshold(vsyncMs?: number): number {
  const v = typeof vsyncMs === 'number' && Number.isFinite(vsyncMs) && vsyncMs > 0 && vsyncMs <= VSYNC_MAX ? vsyncMs : 0;
  return Math.max(JANK_P95_MS, 2 * v + 4);
}
/** The summary the worker rung's governor sees: no long-task signal, and only frames clear of long tasks. */
export function workerJank(s: JankSummary): JankSummary {
  return { ...s, p95: s.p95Free ?? s.p95, longMs: 0 };
}

/** Percentile q (0..1] of the intervals (nearest rank). */
export function pct(intervals: ArrayLike<number>, q: number): number {
  const a = Array.from(intervals).filter((x) => Number.isFinite(x) && x >= 0).sort((x, y) => x - y);
  return a.length ? a[Math.min(a.length - 1, Math.max(0, Math.ceil(a.length * q) - 1))] : 0;
}
/** 95th percentile of the intervals (nearest rank). */
export function p95(intervals: ArrayLike<number>): number { return pct(intervals, 0.95); }

/** The governor's view of one window: sustained-jank is decided by the caller's hold. */
export function isJanky(s: JankSummary): boolean {
  if (!(s.n >= JANK_MIN_SAMPLES) || !(s.windowMs > 0)) return false;
  return s.p95 > jankThreshold(s.vsyncMs) || s.longMs > s.windowMs * JANK_LONG_FRAC;
}

export function describeJank(s: JankSummary): string {
  return s.p95 > jankThreshold(s.vsyncMs) ? `main thread frame p95 ${s.p95.toFixed(0)} ms` : `main thread long tasks ${s.longMs.toFixed(0)} ms in ${(s.windowMs / 1000).toFixed(1)} s`;
}

export interface JankDeps {
  raf: (cb: (t: number) => void) => number;
  caf: (h: number) => void;
  now: () => number;
  onReport: (s: JankSummary) => void;
  windowMs?: number;     // summary window (default 2000)
  reportMs?: number;     // report cadence (default 1000)
}

/** rAF interval + long-task sampler. start()/stop() are idempotent; reset() drops the window (after a downgrade). */
export class JankMeter {
  private t: number[] = []; private d: number[] = [];
  private longs: { t: number; ms: number }[] = [];
  private last = -1; private since = -1; private lastReport = -1;
  private vsync = Infinity;               // display frame time estimate; survives reset()
  private handle = 0; private running = false;
  private po: PerformanceObserver | null = null;
  private readonly windowMs: number; private readonly reportMs: number;

  constructor(private deps: JankDeps) {
    this.windowMs = deps.windowMs ?? 2000; this.reportMs = deps.reportMs ?? 1000;
  }
  get active(): boolean { return this.running; }

  start(): void {
    if (this.running) return;
    this.running = true; this.reset();
    this.observeLongTasks();
    this.handle = this.deps.raf(this.tick);
  }
  stop(): void {
    if (!this.running) return;
    this.running = false;
    this.deps.caf(this.handle);
    try { this.po?.disconnect(); } catch { /* ignore */ }
    this.po = null;
  }
  reset(): void { this.t.length = 0; this.d.length = 0; this.longs.length = 0; this.last = -1; this.since = -1; this.lastReport = -1; }

  /** Feed one long task (also used by tests). */
  longTask(start: number, ms: number): void { if (this.running && Number.isFinite(ms) && ms > 0) this.longs.push({ t: start + ms, ms }); }
  /** Feed one rAF timestamp (the rAF callback; exposed for tests). */
  frame(now: number): void {
    if (this.since < 0) this.since = now;
    if (this.last >= 0) { this.t.push(now); this.d.push(now - this.last); }
    this.last = now;
    const cut = now - this.windowMs;
    let k = 0; while (k < this.t.length && this.t[k] < cut) k++;
    if (k) { this.t.splice(0, k); this.d.splice(0, k); }
    let j = 0; while (j < this.longs.length && this.longs[j].t < cut) j++;
    if (j) this.longs.splice(0, j);
    if (now - this.since < this.windowMs * 0.75) return;            // not a full window yet
    if (this.lastReport >= 0 && now - this.lastReport < this.reportMs) return;
    this.lastReport = now;
    const span = Math.min(this.windowMs, now - this.since);
    if (this.d.length >= JANK_MIN_SAMPLES) this.vsync = Math.min(this.vsync, pct(this.d, 0.1));
    const free: number[] = [];
    for (let i = 0; i < this.d.length; i++) {
      const b = this.t[i], a = b - this.d[i];
      if (!this.longs.some((l) => l.t > a && l.t - l.ms < b)) free.push(this.d[i]);
    }
    this.deps.onReport({ p95: +p95(this.d).toFixed(1), longMs: +this.longs.reduce((a, x) => a + x.ms, 0).toFixed(1), windowMs: Math.round(span), n: this.d.length,
      ...(Number.isFinite(this.vsync) ? { vsyncMs: +this.vsync.toFixed(1) } : {}), p95Free: +p95(free).toFixed(1) });
  }

  private tick = (t: number): void => {
    if (!this.running) return;
    this.frame(t);
    if (this.running) this.handle = this.deps.raf(this.tick);
  };
  private observeLongTasks(): void {
    try {
      if (typeof PerformanceObserver === 'undefined' || !PerformanceObserver.supportedEntryTypes?.includes('longtask')) return;
      this.po = new PerformanceObserver((list) => { for (const e of list.getEntries()) this.longTask(e.startTime, e.duration); });
      this.po.observe({ type: 'longtask', buffered: false });
    } catch { this.po = null; }
  }
}
