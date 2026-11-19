// The frame loop shared by the worker and the main-thread fallback: fixed
// timestep, fps cap, run state, reduced motion, governor, stats. No DOM access.
import type { BgConfig, BgRunState, BgTheme } from '../shared/bg';
import { derivePalette, effectiveIntensity, type Palette } from './palette';
import { Governor } from './governor';
import type { AnyCanvas, Effect, EffectDef } from './effects/types';
import type { RendererKind } from './protocol';

export interface SceneStats { fps: number; frameMs: number; renderer: RendererKind | 'main2d'; tier: 0 | 1 | 2 | 3; downgraded: boolean; reason: string }
export interface SceneHooks {
  raf: (cb: (t: number) => void) => number;
  caf: (h: number) => void;
  now: () => number;
  onStats: (s: SceneStats, frames: number) => void;
  onFail: (reason: string, contextLost: boolean) => void;
  maxFps?: number;            // main-thread fallback: 15
  label?: 'main2d';
}

const STEP = 1 / 60;
const BUDGET_MS = 4;          // DR(h) 4: CPU per frame

export class Scene {
  renderer: RendererKind = 'worker2d';
  private eff: Effect | null = null;
  private gl: WebGL2RenderingContext | null = null;
  private cfg: BgConfig; private theme: BgTheme; private pal: Palette;
  private state: BgRunState = 'run'; private reduced = false;
  private w = 1; private h = 1;
  private rafId = 0; private running = false;
  private lastT = -1; private lastDraw = -1; private acc = 0;
  private frames = 0; private statFrames = 0; private statWork = 0; private statT = -1;
  private gov = new Governor({ budgetMs: BUDGET_MS });
  private burnMs = 0;
  private lost = false;
  private disposed = false;

  constructor(private canvas: AnyCanvas, private def: EffectDef, cfg: BgConfig, theme: BgTheme, private allowGL: boolean, private hooks: SceneHooks) {
    this.cfg = cfg; this.theme = theme; this.pal = derivePalette(theme);
  }

  /** Build the effect: WebGL2 if allowed and offered, else 2D. Throws if neither works. */
  start(w: number, h: number): RendererKind {
    this.w = w; this.h = h;
    this.build();
    this.once();
    return this.renderer;
  }

  private build(): void {
    this.eff?.dispose(); this.eff = null;
    const init = { canvas: this.canvas, cfg: this.cfg, pal: this.pal, w: this.w, h: this.h };
    let err = '';
    if (this.allowGL && this.def.makeGL) {
      try {
        const gl = (this.gl ?? this.canvas.getContext('webgl2', { alpha: false, antialias: false, depth: false, stencil: false, premultipliedAlpha: false, powerPreference: 'low-power', preserveDrawingBuffer: false })) as WebGL2RenderingContext | null;
        if (!gl) throw new Error('webgl2 unavailable');
        if (!this.gl) {
          this.gl = gl;
          (this.canvas as OffscreenCanvas).addEventListener?.('webglcontextlost', ((e: Event) => { e.preventDefault(); this.lost = true; this.stop(); this.hooks.onFail('WebGL context lost', true); }) as EventListener);
          (this.canvas as OffscreenCanvas).addEventListener?.('webglcontextrestored', (() => { this.lost = false; try { this.build(); this.kick(); } catch (e) { this.hooks.onFail(`webgl2 restore: ${(e as Error).message}`, false); } }) as EventListener);
        }
        this.eff = this.def.makeGL(gl, init);
        this.renderer = 'webgl2';
      } catch (e) { err = (e as Error).message || 'webgl2 failed'; this.eff = null; }
    }
    if (!this.eff) {
      if (!this.def.make2d || this.gl) throw new Error(err || 'no 2d renderer for this background');
      const ctx = this.canvas.getContext('2d', { alpha: false }) as CanvasRenderingContext2D | OffscreenCanvasRenderingContext2D | null;
      if (!ctx) throw new Error(err ? `${err}; no 2d context` : 'no 2d context');
      this.eff = this.def.make2d(ctx, init);
      this.renderer = 'worker2d';
    }
    this.eff.resize(this.w, this.h, this.gov.rung.scale);
    this.eff.setPalette(this.pal, this.intensity());
  }

  private intensity(): number { return effectiveIntensity(this.pal, this.cfg.intensity) * (this.state === 'dim' ? 0.6 : 1); }

  setConfig(cfg: BgConfig): void {
    const rebuild = cfg.seed !== this.cfg.seed || JSON.stringify(cfg.params) !== JSON.stringify(this.cfg.params);
    this.cfg = cfg;
    this.gov.reset();
    if (rebuild) { try { this.build(); } catch (e) { this.hooks.onFail((e as Error).message, false); return; } }
    else { this.eff?.resize(this.w, this.h, this.gov.rung.scale); this.eff?.setPalette(this.pal, this.intensity()); }
    this.once();
  }
  setTheme(theme: BgTheme): void {
    this.theme = theme; this.pal = derivePalette(theme);
    this.eff?.setPalette(this.pal, this.intensity());
    this.once();
  }
  setState(state: BgRunState, reduced: boolean): void {
    const was = this.state;
    this.state = state; this.reduced = reduced;
    if (was !== state) this.eff?.setPalette(this.pal, this.intensity());
    if (state === 'pause') { this.stop(); this.gov.pause(); this.hooks.onStats(this.stats(0, 0), this.frames); return; }   // exact count at pause
    if (reduced) { this.stop(); this.settleSoon(); return; }
    this.kick();
  }
  resize(w: number, h: number): void {
    this.w = w; this.h = h;
    this.eff?.resize(w, h, this.gov.rung.scale);
    this.once();
  }
  burn(ms: number): void { this.burnMs = ms; }
  dispose(): void { this.disposed = true; this.stop(); if (this.settleTimer !== null) clearTimeout(this.settleTimer); this.eff?.dispose(); this.eff = null; this.gl?.getExtension('WEBGL_lose_context')?.loseContext(); }

  /** Redraw once when not animating (paused / reduced) so config and theme changes show. */
  private once(): void {
    if (this.running || this.disposed || this.lost) return;
    if (this.reduced) this.settleSoon();
    else if (this.state === 'pause') { this.eff?.draw(); }
    else this.kick();
  }
  /** init, state, theme and resize messages arrive in a burst: settle (expensive) once for all of them. */
  private settleTimer: ReturnType<typeof setTimeout> | null = null;
  private settleSoon(): void {
    if (this.settleTimer !== null) return;
    this.settleTimer = setTimeout(() => { this.settleTimer = null; if (!this.disposed && this.reduced) this.settledFrame(); }, 40);
  }
  private settledFrame(): void {
    if (!this.eff || this.lost) return;
    this.eff.settle(); this.eff.draw();
    this.frames++;
    this.hooks.onStats(this.stats(0, 0), this.frames);
  }
  private stats(fps: number, ms: number): SceneStats {
    const r = this.gov.rung;
    return { fps, frameMs: ms, renderer: this.hooks.label ?? this.renderer, tier: r.tier, downgraded: this.gov.downgraded, reason: this.gov.reason };
  }
  private kick(): void {
    if (this.running || this.disposed || this.lost || this.state === 'pause' || this.reduced || !this.eff) return;
    this.running = true; this.lastT = -1; this.lastDraw = -1; this.statT = -1;
    this.rafId = this.hooks.raf(this.frame);
  }
  private stop(): void {
    if (this.running) this.hooks.caf(this.rafId);
    this.running = false;
  }
  private frame = (t: number): void => {
    if (!this.running || !this.eff) return;
    const r = this.gov.rung;
    let fps: number = this.cfg.fpsCap;
    if (r.fps) fps = Math.min(fps, r.fps);
    if (this.state === 'dim') fps = Math.min(fps, 15);
    if (this.hooks.maxFps) fps = Math.min(fps, this.hooks.maxFps);
    const interval = 1000 / fps;
    if (this.lastDraw >= 0 && t - this.lastDraw < interval - 2) { this.rafId = this.hooks.raf(this.frame); return; }
    const dt = this.lastT < 0 ? STEP : Math.min(0.1, (t - this.lastT) / 1000);
    this.lastT = t; this.lastDraw = t;
    const t0 = this.hooks.now();
    this.acc += dt * this.cfg.speed;
    let n = 0;
    while (this.acc >= STEP && n < 6) { this.eff.step(STEP); this.acc -= STEP; n++; }
    if (n === 6) this.acc = 0;
    this.eff.draw();
    if (this.burnMs > 0) { const end = this.hooks.now() + this.burnMs; while (this.hooks.now() < end) { /* debug: forced heavy frame */ } }
    const work = this.hooks.now() - t0;
    this.frames++; this.statFrames++; this.statWork += work;
    if (this.gov.sample(work, t)) {
      if (this.gov.rung.static) { this.stop(); this.hooks.onStats(this.stats(0, work), this.frames); return; }
      this.eff.resize(this.w, this.h, this.gov.rung.scale);
      this.hooks.onStats(this.stats(0, work), this.frames);                  // report a downgrade at once
    }
    if (this.statT < 0) this.statT = t;
    else if (t - this.statT >= 500) {
      const s = this.stats(+(this.statFrames * 1000 / (t - this.statT)).toFixed(1), +(this.statWork / this.statFrames).toFixed(2));
      this.hooks.onStats(s, this.frames);
      this.statT = t; this.statFrames = 0; this.statWork = 0;
    }
    if (this.running) this.rafId = this.hooks.raf(this.frame);
  };
}
