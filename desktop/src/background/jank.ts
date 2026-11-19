// Main-thread jank feedback for the governor. The worker governor only sees its own CPU time per frame; on a
// software rasteriser (and for GPU/raster-bound effects) the cost lands on the compositor and shows up as late
// main-thread frames instead. JankMeter samples rAF intervals and long tasks on the main thread and reports a
// summary about once a second; isJanky() is the policy the worker governor applies (with its 2 s hold).

export interface JankSummary { p95: number; longMs: number; windowMs: number; n: number }

export const JANK_P95_MS = 34;          // 95th percentile frame interval above two 60 Hz vsyncs
export const JANK_LONG_FRAC = 0.25;     // or a quarter of the window spent in long tasks
export const JANK_MIN_SAMPLES = 20;

/** 95th percentile of the intervals (nearest rank). */
export function p95(intervals: ArrayLike<number>): number {
  const a = Array.from(intervals).filter((x) => Number.isFinite(x) && x >= 0).sort((x, y) => x - y);
  return a.length ? a[Math.min(a.length - 1, Math.ceil(a.length * 0.95) - 1)] : 0;
}

/** The governor's view of one window: sustained-jank is decided by the caller's hold. */
export function isJanky(s: JankSummary): boolean {
  if (!(s.n >= JANK_MIN_SAMPLES) || !(s.windowMs > 0)) return false;
  return s.p95 > JANK_P95_MS || s.longMs > s.windowMs * JANK_LONG_FRAC;
}

export function describeJank(s: JankSummary): string {
  return s.p95 > JANK_P95_MS ? `main thread frame p95 ${s.p95.toFixed(0)} ms` : `main thread long tasks ${s.longMs.toFixed(0)} ms in ${(s.windowMs / 1000).toFixed(1)} s`;
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
    this.deps.onReport({ p95: +p95(this.d).toFixed(1), longMs: +this.longs.reduce((a, x) => a + x.ms, 0).toFixed(1), windowMs: Math.round(span), n: this.d.length });
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
