import type { BgConfig } from '../../shared/bg';
import type { Palette } from '../palette';

export type Ctx2D = CanvasRenderingContext2D | OffscreenCanvasRenderingContext2D;
export type AnyCanvas = OffscreenCanvas | HTMLCanvasElement;

/** A running effect. The host owns the loop, fixed timestep and canvas sizing. */
export interface Effect {
  /** Viewport is w x h css px; the governor allows at most `scaleCap` of it. The effect sizes its own canvas. */
  resize(w: number, h: number, scaleCap: number): void;
  setPalette(p: Palette, intensity: number): void;
  /** Advance the simulation by dt seconds of designed time (already multiplied by speed). */
  step(dt: number): void;
  draw(): void;
  /** Advance to a representative, settled look (reduced motion, thumbnails). */
  settle(): void;
  dispose(): void;
}

export interface EffectInit { canvas: AnyCanvas; cfg: BgConfig; pal: Palette; w: number; h: number }
export interface EffectDef {
  /** Base render scale (fraction of css px). */
  scale: number;
  make2d?: (ctx: Ctx2D, init: EffectInit) => Effect;
  makeGL?: (gl: WebGL2RenderingContext, init: EffectInit) => Effect;
}

/** Size a canvas to css size x scale (at least 1 px); returns true when it changed. */
export function fitCanvas(c: AnyCanvas, w: number, h: number, scale: number): boolean {
  const pw = Math.max(1, Math.round(w * scale)), ph = Math.max(1, Math.round(h * scale));
  if (c.width === pw && c.height === ph) return false;
  c.width = pw; c.height = ph;
  return true;
}
