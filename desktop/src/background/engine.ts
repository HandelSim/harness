// Main-thread side of the background engine. Owns everything inside `root`:
// one CSS layer stack for static backgrounds, one canvas per dynamic config
// (transferred to the module worker), cross-fades between them.
import type { BgConfig, BgCreateOpts, BgDescriptor, BgEngine, BgRunState, BgStats, BgTheme } from '../shared/bg';
import { CATALOG, descriptor, normalizeConfig, DEFAULT_BG } from './catalog';
import { derivePalette, DEFAULT_THEME } from './palette';
import { staticSpec, staticCss } from './effects/static';
import { parseFromWorker, type ToWorker } from './protocol';
import { MainRunner } from './mainrunner';
import { EFFECTS } from './effects/registry';
import { renderThumb } from './thumb';
import { probeGpu, lowPowerActive, lowPowerPlan, isLowPowerMode, LOW_POWER_REASON, type GpuInfo, type LowPowerMode } from './gpu';
import { JankMeter, type JankSummary } from './jank';

type Rung = 'worker' | 'main2d' | 'css';
interface Layer { id: number; el: HTMLElement; cfg: BgConfig; kind: 'css' | 'worker' | 'main2d'; main?: MainRunner; canvas?: HTMLCanvasElement }

const sameJson = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

export function createBackground(root: HTMLElement, opts: BgCreateOpts): BgEngine {
  return new Engine(root, opts);
}

class Engine implements BgEngine {
  private root: HTMLElement;
  private opts: BgCreateOpts;
  private theme: BgTheme = DEFAULT_THEME;
  private runState: BgRunState = 'run';
  private reduced = false;
  private hidden = typeof document !== 'undefined' && document.hidden;
  private committed: BgConfig = DEFAULT_BG;
  private previewing: BgConfig | null = null;
  private layer: Layer | null = null;
  private nextId = 1;
  private worker: Worker | null = null;
  private workerBroken = '';
  private gpuBroken = '';                  // set after repeated context loss / no webgl2
  private glBlocked = new Set<string>();   // ids whose GL path failed (e.g. no float targets)
  private lastInitGL = new Map<number, boolean>();
  private lossTimes: number[] = [];
  private statsCbs = new Set<(s: BgStats) => void>();
  private lastStats: BgStats = { fps: 0, frameMs: 0, renderer: 'css', tier: 3, downgraded: false, reason: '' };
  private thumbs = new Map<string, Promise<string>>();
  private thumbReq = 1;
  private thumbWait = new Map<number, (b: Blob | null) => void>();
  private ro: ResizeObserver | null = null;
  private onVis = () => { this.hidden = document.hidden; this.pushState(); };
  private disposed = false;
  private size = { w: 1, h: 1 };
  private lowMode: LowPowerMode = 'auto';
  private jankMeter: JankMeter | null = null;
  private lastReason = '';

  constructor(root: HTMLElement, opts: BgCreateOpts) {
    this.root = root; this.opts = opts;
    const st = root.style;
    if (!st.position || st.position === 'static') st.position = getComputedStyle(root).position === 'static' ? 'fixed' : st.position;
    if (!st.inset) st.inset = '0';
    st.overflow = 'hidden'; st.pointerEvents = 'none'; st.contain = 'strict';
    root.setAttribute('aria-hidden', 'true');
    document.addEventListener('visibilitychange', this.onVis);
    this.measure();
    if (typeof ResizeObserver !== 'undefined') {
      this.ro = new ResizeObserver(() => this.onResize());
      this.ro.observe(root);
    }
    this.apply(this.committed, 0);
  }

  catalog(): BgDescriptor[] { return CATALOG.map((d) => ({ ...d, params: d.params.map((p) => ({ ...p })) })); }

  async set(config: BgConfig, o?: { fadeMs?: number }): Promise<void> {
    this.committed = normalizeConfig(config);
    this.previewing = null;
    await this.apply(this.committed, o?.fadeMs ?? 400);
  }
  preview(config: BgConfig | null): void {
    this.previewing = config ? normalizeConfig(config) : null;
    void this.apply(this.previewing ?? this.committed, 250);
  }
  setTheme(theme: BgTheme): void {
    if (sameJson(theme, this.theme)) return;
    this.theme = { ...theme };
    this.thumbs.clear();
    this.post({ t: 'theme', theme: this.theme });
    const l = this.layer;
    if (l?.kind === 'css') this.paintCss(l.el, l.cfg);
    l?.main?.setTheme(this.theme);
  }
  setRunState(state: BgRunState): void { this.runState = state; this.pushState(); }
  setReducedMotion(on: boolean): void { this.reduced = !!on; this.pushState(); }
  onStats(cb: (s: BgStats) => void): () => void {
    this.statsCbs.add(cb); cb(this.lastStats);
    return () => { this.statsCbs.delete(cb); };
  }
  /** No-GPU policy (not part of BgEngine): 'auto' = on when probeGpu() reports a software rasteriser.
   *  Changing it rebuilds the live layer. */
  setLowPower(mode: LowPowerMode): void {
    const m = isLowPowerMode(mode) ? mode : 'auto';
    if (m === this.lowMode) return;
    const was = this.lowActive();
    this.lowMode = m;
    if (was !== this.lowActive() && this.layer) void this.apply(this.layer.cfg, 0, true);
  }
  lowPower(): { mode: LowPowerMode; active: boolean; gpu: GpuInfo } {
    return { mode: this.lowMode, active: this.lowActive(), gpu: probeGpu() };
  }
  private lowActive(): boolean { return this.lowMode === 'on' || (this.lowMode === 'auto' && lowPowerActive('auto', probeGpu())); }
  /** Stats of a canvas layer running under the low-power limits say so (unless something more specific applies). */
  private lowStats(s: BgStats): BgStats {
    if (s.downgraded || s.reason || !this.layer || this.layer.kind === 'css' || this.limits(this.layer.cfg).maxFps === undefined) return s;
    return { ...s, downgraded: true, reason: LOW_POWER_REASON };
  }
  private lowStatic(cfg: BgConfig): boolean {
    const d = descriptor(cfg.id);
    return !!d && this.lowActive() && lowPowerPlan(d).mode === 'static';
  }
  /** Debug hook (demo / e2e): make each worker frame burn this many ms. Not part of BgEngine. */
  debugBurn(ms: number): void { this.post({ t: 'debug', burnMs: ms }); this.layer?.main?.burn(ms); }

  thumbnail(id: string, w: number, h: number, theme: BgTheme): Promise<string> {
    const key = `${id}|${w}x${h}|${JSON.stringify(theme)}`;
    let p = this.thumbs.get(key);
    if (!p) {
      p = this.makeThumb(id, w, h, theme).catch((e) => { this.thumbs.delete(key); throw e; });
      this.thumbs.set(key, p);
    }
    return p;
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.jankMeter?.stop(); this.jankMeter = null;
    document.removeEventListener('visibilitychange', this.onVis);
    this.ro?.disconnect();
    this.layer?.main?.dispose();
    this.worker?.terminate(); this.worker = null;
    for (const p of this.thumbs.values()) p.then((u) => URL.revokeObjectURL(u), () => {});
    this.thumbs.clear();
    this.root.replaceChildren();
    this.statsCbs.clear();
  }

  // ---------------------------------------------------------------- internals
  private effState(): { state: BgRunState; reduced: boolean } {
    return { state: this.hidden ? 'pause' : this.runState, reduced: this.reduced };
  }
  private pushState(): void {
    const s = this.effState();
    this.post({ t: 'state', ...s });
    this.layer?.main?.setState(s.state, s.reduced);
    const l = this.layer;
    if (l?.kind === 'css') this.cssMotion(l.el, l.cfg);
    this.syncJank();
  }
  /** Measure main-thread jank only while a canvas layer animates (not paused, hidden, reduced or CSS). */
  private syncJank(): void {
    const l = this.layer, s = this.effState();
    const want = !this.disposed && !!l && l.kind !== 'css' && s.state !== 'pause' && !s.reduced && typeof requestAnimationFrame === 'function';
    if (!want) { this.jankMeter?.stop(); return; }
    this.jankMeter ??= new JankMeter({
      raf: (cb) => requestAnimationFrame(cb), caf: (h) => cancelAnimationFrame(h), now: () => performance.now(),
      onReport: (r) => this.onJank(r),
    });
    this.jankMeter.start();
  }
  private onJank(r: JankSummary): void {
    const l = this.layer;
    if (!l) return;
    this.root.dataset.bgJank = `${r.p95}/${r.longMs}`;
    if (l.kind === 'worker') this.post({ t: 'jank', id: l.id, ...r });
    else l.main?.jank(r);
  }
  private measure(): void {
    const r = this.root.getBoundingClientRect();
    this.size = { w: Math.max(1, Math.round(r.width || innerWidth)), h: Math.max(1, Math.round(r.height || innerHeight)) };
  }
  private onResize(): void {
    this.measure();
    const l = this.layer;
    if (!l) return;
    if (l.kind === 'worker') this.post({ t: 'resize', id: l.id, w: this.size.w, h: this.size.h });
    l.main?.resize(this.size.w, this.size.h);
  }
  private emit(s: BgStats): void {
    this.lastStats = s;
    for (const cb of this.statsCbs) { try { cb(s); } catch { /* listener errors stay local */ } }
  }
  private post(m: ToWorker, transfer?: Transferable[]): void {
    if (!this.worker) return;
    try { this.worker.postMessage(m, transfer ?? []); } catch { /* worker gone */ }
  }

  private ensureWorker(): Worker | null {
    if (this.worker || this.workerBroken) return this.worker;
    if (typeof Worker === 'undefined' || typeof HTMLCanvasElement === 'undefined'
      || !('transferControlToOffscreen' in HTMLCanvasElement.prototype)) { this.workerBroken = 'no OffscreenCanvas'; return null; }
    try {
      const w = new Worker(this.opts.workerUrl, { type: 'module', name: 'harness-bg' });
      w.onmessage = (e) => this.onWorker(e.data);
      w.onerror = (e) => { e.preventDefault?.(); this.workerFailed(`worker error: ${e.message || 'load failed'}`); };
      this.worker = w;
      w.postMessage({ t: 'theme', theme: this.theme } satisfies ToWorker);
      w.postMessage({ t: 'state', ...this.effState() } satisfies ToWorker);
      return w;
    } catch (e) {
      this.workerBroken = `worker: ${(e as Error).message}`;
      return null;
    }
  }
  private workerFailed(reason: string): void {
    this.workerBroken = reason;
    this.worker?.terminate(); this.worker = null;
    for (const r of this.thumbWait.values()) r(null);
    this.thumbWait.clear();
    const l = this.layer;
    if (l && l.kind === 'worker') void this.apply(l.cfg, 0, true, false, reason);
  }

  private onWorker(raw: unknown): void {
    const m = parseFromWorker(raw);
    if (!m) return;
    if (m.t === 'thumb') { this.thumbWait.get(m.req)?.(m.blob); this.thumbWait.delete(m.req); return; }
    const l = this.layer;
    if (!l || l.id !== m.id) return;
    if (m.t === 'stats') {
      this.root.dataset.bgFrames = String(m.frames);
      if (m.reason !== this.lastReason) { this.lastReason = m.reason; if (m.downgraded) this.jankMeter?.reset(); }   // new rung: fresh jank window
      this.emit(this.lowStats({ fps: m.fps, frameMs: m.frameMs, renderer: m.renderer, tier: m.tier, downgraded: m.downgraded || !!this.gpuBroken, reason: m.reason || this.gpuBroken }));
      if (m.tier === 3) void this.apply(l.cfg, 400, true, true, m.reason);       // governor reached static
    } else if (m.t === 'ready') {
      this.root.dataset.bgRenderer = m.renderer;
    } else if (m.t === 'fail') {
      if (m.contextLost) {
        const now = performance.now();
        this.lossTimes = this.lossTimes.filter((t) => now - t < 60_000).concat(now);
        if (this.lossTimes.length < 2) return;                       // the worker rebuilds on restore
        this.gpuBroken = 'WebGL context lost twice in 60 s';
      } else if (this.lastInitGL.get(m.id)) {
        if (/webgl2 unavailable/.test(m.reason)) this.gpuBroken = m.reason; else this.glBlocked.add(l.cfg.id);
      } else { void this.apply(l.cfg, 0, true, true, m.reason); return; }       // 2D failed in the worker too
      void this.apply(l.cfg, 0, true, false, m.reason);
    }
  }

  /** Pick the rung for a config: worker (GL or 2D inside), main-thread 2D, or CSS static. */
  private pickRung(cfg: BgConfig, forceCss: boolean): Rung {
    const d = descriptor(cfg.id), def = EFFECTS[cfg.id];
    if (forceCss || !d || !d.dynamic || !def || this.lowStatic(cfg)) return 'css';
    if (this.ensureWorker() && (def.make2d || (def.makeGL && this.glAllowed(cfg.id)))) return 'worker';
    return def.make2d ? 'main2d' : 'css';
  }
  private glAllowed(id: string): boolean { return !this.gpuBroken && !this.glBlocked.has(id); }

  private async apply(cfg: BgConfig, fadeMs: number, fallback = false, forceCss = false, reason = ''): Promise<void> {
    if (this.disposed) return;
    const cur = this.layer;
    if (!fallback && cur && sameJson(cur.cfg, cfg)) return;
    const rung = this.pickRung(cfg, forceCss);

    // Same kind, same id: update in place (no new canvas, no fade).
    if (!fallback && cur && cur.cfg.id === cfg.id && cur.kind === rung) {
      cur.cfg = cfg;
      if (rung === 'css') this.paintCss(cur.el, cfg);
      else if (rung === 'worker') this.post({ t: 'config', id: cur.id, config: cfg });
      else cur.main?.setConfig(cfg);
      return;
    }

    const id = this.nextId++;
    let layer: Layer;
    if (rung === 'css') {
      const el = document.createElement('div');
      layer = { id, el, cfg, kind: 'css' };
      this.paintCss(el, cfg);
      const why = descriptor(cfg.id)?.dynamic ? (reason || this.gpuBroken || (this.lowStatic(cfg) ? LOW_POWER_REASON : '') || this.workerBroken || 'static fallback') : '';
      this.emit({ fps: 0, frameMs: 0, renderer: 'css', tier: descriptor(cfg.id)?.dynamic ? 3 : 0, downgraded: !!why, reason: why });
      this.root.dataset.bgRenderer = 'css';
    } else {
      const canvas = document.createElement('canvas');
      Object.assign(canvas.style, { position: 'absolute', inset: '0', width: '100%', height: '100%', display: 'block' });
      const el = document.createElement('div');
      el.appendChild(canvas);
      // Static base under the canvas so the cross-fade and the first frames never flash.
      el.style.background = staticCss(staticSpec({ ...cfg, id: 'solid' }, derivePalette(this.theme))).base;
      layer = { id, el, cfg, kind: rung, canvas };
      if (rung === 'worker') {
        const off = canvas.transferControlToOffscreen();
        const allowGL = this.glAllowed(cfg.id);
        this.lastInitGL.set(id, allowGL);
        this.post({ t: 'init', id, canvas: off, w: this.size.w, h: this.size.h, config: cfg, allowGL, ...this.limits(cfg) }, [off]);
      } else {
        try { layer.main = new MainRunner(canvas, cfg, this.theme, (s) => { this.root.dataset.bgFrames = String(s.frames); this.emit(this.lowStats(s.stats)); }, this.limits(cfg));
        } catch (e) { return this.apply(cfg, 0, true, true, (e as Error).message); }
        layer.main.resize(this.size.w, this.size.h);
        const s = this.effState(); layer.main.setState(s.state, s.reduced);
      }
    }
    Object.assign(layer.el.style, { position: 'absolute', inset: '0', opacity: '0' });
    layer.el.dataset.bg = cfg.id;
    this.root.appendChild(layer.el);
    this.layer = layer;
    this.root.dataset.bgId = cfg.id;
    this.lastReason = '';
    this.jankMeter?.reset();                                   // new layer: fresh jank window
    this.syncJank();

    const ms = this.reduced || !cur ? 0 : Math.max(0, fadeMs);
    await new Promise<void>((resolve) => {
      if (ms === 0) { layer.el.style.opacity = '1'; resolve(); }
      else {
        layer.el.style.transition = `opacity ${ms}ms cubic-bezier(0.65, 0, 0.35, 1)`;
        requestAnimationFrame(() => { layer.el.style.opacity = '1'; });
        setTimeout(resolve, ms + 30);
      }
    });
    if (cur) this.retire(cur);
    // a later apply may have replaced this layer during the fade; retire any stragglers
    for (const child of Array.from(this.root.children)) if (child !== this.layer?.el && child !== layer.el) child.remove();
  }

  /** Low power: frame-rate cap and render-scale multiplier sent with the layer's init. */
  private limits(cfg: BgConfig): { maxFps?: number; scaleMul?: number } {
    const d = descriptor(cfg.id);
    if (!d || !this.lowActive()) return {};
    const p = lowPowerPlan(d);
    return p.mode === 'reduced' ? { maxFps: Math.min(cfg.fpsCap, p.fpsCap), scaleMul: p.scaleMul } : {};
  }

  private retire(l: Layer): void {
    if (l === this.layer) return;
    if (l.kind === 'worker') this.post({ t: 'drop', id: l.id });
    l.main?.dispose();
    l.el.remove();
  }

  private paintCss(el: HTMLElement, cfg: BgConfig): void {
    const css = staticCss(staticSpec(cfg, derivePalette(this.theme)));
    let blooms = el.querySelector<HTMLElement>('[data-l="blooms"]');
    if (!blooms) {
      el.replaceChildren();
      for (const n of ['blooms', 'grid', 'vignette']) {
        const d = document.createElement('div');
        d.dataset.l = n;
        Object.assign(d.style, { position: 'absolute', inset: n === 'blooms' ? '-10%' : '0' });
        el.appendChild(d);
      }
      blooms = el.querySelector<HTMLElement>('[data-l="blooms"]')!;
    }
    el.style.background = css.base;
    blooms.style.backgroundImage = css.blooms;
    const grid = el.querySelector<HTMLElement>('[data-l="grid"]')!;
    grid.style.backgroundImage = css.grid; grid.style.backgroundSize = css.gridSize;
    grid.style.setProperty('mask-image', css.gridMask); grid.style.setProperty('-webkit-mask-image', css.gridMask);
    el.querySelector<HTMLElement>('[data-l="vignette"]')!.style.backgroundImage = css.vignette;
    this.cssMotion(el, cfg);
  }

  /** Mesh "drift": a compositor-only transform animation; paused with the engine, off under reduced motion. */
  private cssMotion(el: HTMLElement, cfg: BgConfig): void {
    const blooms = el.querySelector<HTMLElement>('[data-l="blooms"]');
    if (!blooms) return;
    const want = cfg.id === 'mesh' && cfg.params.drift === true && !this.reduced;
    let anim = blooms.getAnimations?.()[0];
    if (!want) { anim?.cancel(); return; }
    if (!anim && typeof blooms.animate === 'function') {
      anim = blooms.animate(
        [{ transform: 'translate(0,0) rotate(0deg) scale(1)' }, { transform: 'translate(3%, -2%) rotate(4deg) scale(1.05)' }, { transform: 'translate(0,0) rotate(0deg) scale(1)' }],
        { duration: 50_000 / Math.max(0.1, cfg.speed), iterations: Infinity, easing: 'ease-in-out' });
    }
    if (!anim) return;
    if (this.effState().state === 'pause') anim.pause(); else anim.play();
  }

  private async makeThumb(id: string, w: number, h: number, theme: BgTheme): Promise<string> {
    const d = descriptor(id);
    if (!d) throw new Error(`unknown background ${id}`);
    const cfg = normalizeConfig({ ...DEFAULT_BG, id, params: {} , seed: this.committed.id === id ? this.committed.seed : DEFAULT_BG.seed });
    const worker = d.dynamic ? this.ensureWorker() : null;
    if (worker) {
      const req = this.thumbReq++;
      const blob = await new Promise<Blob | null>((resolve) => {
        this.thumbWait.set(req, resolve);
        this.post({ t: 'thumb', req, config: cfg, w, h, theme });
        setTimeout(() => { if (this.thumbWait.delete(req)) resolve(null); }, 10_000);
      });
      if (blob) return URL.createObjectURL(blob);
    }
    // no worker: same renderer on the main thread (OffscreenCanvas exists there too)
    const blob = await renderThumb(cfg, theme, w, h);
    return URL.createObjectURL(blob);
  }
}
