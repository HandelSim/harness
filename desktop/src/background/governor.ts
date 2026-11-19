// Pure frame-time governor: rolling mean over 60 frames; over 1.5x budget for 2 s
// steps one rung down the ladder. It never climbs back by itself; reset() (a
// config change) starts again at the top.
export interface Rung { scale: number; fps: number | null; static: boolean; tier: 0 | 1 | 2 | 3 }
// scale caps first, then fps caps, then static.
export const LADDER: Rung[] = [
  { scale: 1, fps: null, static: false, tier: 0 },
  { scale: 0.5, fps: null, static: false, tier: 1 },
  { scale: 0.35, fps: null, static: false, tier: 1 },
  { scale: 0.25, fps: null, static: false, tier: 1 },
  { scale: 0.25, fps: 30, static: false, tier: 2 },
  { scale: 0.25, fps: 15, static: false, tier: 2 },
  { scale: 0.25, fps: 15, static: true, tier: 3 },
];

export interface GovernorOpts { budgetMs: number; window?: number; overFactor?: number; holdMs?: number }

export class Governor {
  readonly budgetMs: number; readonly window: number; readonly overFactor: number; readonly holdMs: number;
  level = 0;
  reason = '';
  private buf: Float64Array; private n = 0; private i = 0; private sum = 0;
  private overSince: number | null = null;

  constructor(o: GovernorOpts) {
    this.budgetMs = o.budgetMs; this.window = o.window ?? 60; this.overFactor = o.overFactor ?? 1.5; this.holdMs = o.holdMs ?? 2000;
    this.buf = new Float64Array(this.window);
  }
  get rung(): Rung { return LADDER[this.level]; }
  get downgraded(): boolean { return this.level > 0; }
  get mean(): number { return this.n ? this.sum / this.n : 0; }

  reset(): void { this.level = 0; this.reason = ''; this.clearWindow(); }
  /** Call when the loop pauses: time spent paused must not count as "over budget". */
  pause(): void { this.overSince = null; }
  private clearWindow(): void { this.buf.fill(0); this.n = 0; this.i = 0; this.sum = 0; this.overSince = null; }

  /** Record one frame's work time. Returns true when it stepped down a rung on this call. */
  sample(frameMs: number, now: number): boolean {
    if (!Number.isFinite(frameMs) || frameMs < 0) return false;
    if (this.n === this.window) this.sum -= this.buf[this.i]; else this.n++;
    this.buf[this.i] = frameMs; this.sum += frameMs; this.i = (this.i + 1) % this.window;
    if (this.level >= LADDER.length - 1) return false;
    if (this.n < Math.min(this.window, 10) || this.mean <= this.budgetMs * this.overFactor) { this.overSince = null; return false; }
    if (this.overSince === null) { this.overSince = now; return false; }
    if (now - this.overSince < this.holdMs) return false;
    return this.downgrade(`frame ${this.mean.toFixed(1)} ms over ${(this.budgetMs * this.overFactor).toFixed(1)} ms budget`);
  }
  /** Step down one rung now (also used on repeated context loss). */
  downgrade(reason: string): boolean {
    if (this.level >= LADDER.length - 1) return false;
    this.level++; this.reason = reason; this.clearWindow();
    return true;
  }
}
