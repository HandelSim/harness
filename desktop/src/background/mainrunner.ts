// Main-thread 2D fallback (no OffscreenCanvas / worker): same Scene, capped at 15 fps.
import type { BgConfig, BgRunState, BgStats, BgTheme } from '../shared/bg';
import { Scene } from './host';
import { EFFECTS } from './effects/registry';
import type { JankSummary } from './jank';

export class MainRunner {
  private scene: Scene;
  constructor(canvas: HTMLCanvasElement, cfg: BgConfig, theme: BgTheme, onStats: (s: { stats: BgStats; frames: number }) => void,
    limits: { maxFps?: number; scaleMul?: number } = {}) {
    const def = EFFECTS[cfg.id];
    if (!def?.make2d) throw new Error(`no 2d renderer for ${cfg.id}`);
    this.scene = new Scene(canvas, def, cfg, theme, false, {
      raf: (cb) => requestAnimationFrame(cb), caf: (h) => cancelAnimationFrame(h), now: () => performance.now(),
      onStats: (s, frames) => onStats({ stats: { ...s, renderer: 'main2d' }, frames }),
      onFail: () => { /* 2D on the main thread has no context loss path */ },
      maxFps: Math.min(15, limits.maxFps ?? 15), scaleMul: limits.scaleMul, label: 'main2d',
    });
    this.scene.start(Math.max(1, canvas.clientWidth || innerWidth), Math.max(1, canvas.clientHeight || innerHeight));
  }
  setConfig(cfg: BgConfig): void { this.scene.setConfig(cfg); }
  setTheme(t: BgTheme): void { this.scene.setTheme(t); }
  setState(s: BgRunState, reduced: boolean): void { this.scene.setState(s, reduced); }
  resize(w: number, h: number): void { this.scene.resize(w, h); }
  burn(ms: number): void { this.scene.burn(ms); }
  jank(s: JankSummary): void { this.scene.jank(s); }
  dispose(): void { this.scene.dispose(); }
}
