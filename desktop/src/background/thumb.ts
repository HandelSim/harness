// Thumbnails: a settled frame drawn into a small OffscreenCanvas, encoded with convertToBlob.
// Used by the worker (normal path) and by the main thread when no worker is available.
import type { BgConfig, BgTheme } from '../shared/bg';
import { derivePalette } from './palette';
import { descriptor } from './catalog';
import { EFFECTS } from './effects/registry';
import { staticSpec, drawStatic2d } from './effects/static';

let glCanvas: OffscreenCanvas | null = null;      // one shared GL context for GL-only thumbnails

export async function renderThumb(config: BgConfig, theme: BgTheme, w: number, h: number): Promise<Blob> {
  const pal = derivePalette(theme);
  const d = descriptor(config.id), def = EFFECTS[config.id];
  const out = new OffscreenCanvas(w, h);
  const octx = out.getContext('2d', { alpha: false });
  if (!octx) throw new Error('no 2d context');
  if (d?.dynamic && def) {
    const cw = w * 2, ch = h * 2;                  // render as if the viewport were 2x the thumb, then downscale (3x cost 2.25x the pixels for no visible gain at thumb size)
    let src: OffscreenCanvas;
    let eff;
    if (def.make2d) {
      src = new OffscreenCanvas(1, 1);
      const ctx = src.getContext('2d', { alpha: false });
      if (!ctx) throw new Error('no 2d context');
      eff = def.make2d(ctx, { canvas: src, cfg: config, pal, w: cw, h: ch });
    } else {
      glCanvas ??= new OffscreenCanvas(1, 1);
      src = glCanvas;
      const gl = src.getContext('webgl2', { alpha: false, preserveDrawingBuffer: true }) as WebGL2RenderingContext | null;
      if (!gl) throw new Error('webgl2 unavailable');
      eff = def.makeGL!(gl, { canvas: src, cfg: config, pal, w: cw, h: ch });
    }
    try {
      eff.resize(cw, ch, 1);
      eff.setPalette(pal, config.intensity * pal.maxIntensity);
      eff.settle(); eff.draw();
      octx.imageSmoothingEnabled = true; octx.imageSmoothingQuality = 'high';
      octx.drawImage(src, 0, 0, w, h);
    } finally { eff.dispose(); }
  } else {
    drawStatic2d(octx, w, h, staticSpec(config, pal));
  }
  return out.convertToBlob({ type: 'image/png' });
}
