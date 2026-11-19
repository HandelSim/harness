// B5: particles advected by the curl of 3D simplex noise; trails by a decaying intensity buffer.
import type { Effect, Ctx2D, EffectInit } from './types';
import { fitCanvas } from './types';
import { type Palette, ramp } from '../palette';
import { mulberry32, deriveSeed } from '../prng';
import { simplex3, curl2 } from '../noise';

export function makeFlow2d(ctx: Ctx2D, init: EffectInit): Effect {
  const N = Math.max(100, Math.min(8000, Number(init.cfg.params.particles) || 2000));
  const zoom = Number(init.cfg.params.zoom) || 1;
  const trail = Math.min(0.995, Math.max(0.5, Number(init.cfg.params.trail) || 0.92));
  const rng = mulberry32(deriveSeed(init.cfg.seed, 'flow'));
  const noise = simplex3(deriveSeed(init.cfg.seed, 'flow-noise'));
  const X = new Float32Array(N), Y = new Float32Array(N), PX = new Float32Array(N), PY = new Float32Array(N), life = new Uint16Array(N);
  let W = 1, H = 1, t = rng() * 100, pal: Palette = init.pal, k = 0.5;
  // Trails live in a CPU intensity buffer (decay + line deposits) shown with one putImageData per frame.
  // Stroking thousands of canvas paths was raster-bound (and janked the page) on software GL.
  let I = new Float32Array(1), img: ImageData | null = null, lut = new Uint8ClampedArray(256 * 3), dirty = true;
  const spawn = (i: number) => { X[i] = rng() * W; Y[i] = rng() * H; PX[i] = X[i]; PY[i] = Y[i]; life[i] = 200 + ((rng() * 400) | 0); };
  const advance = (dt: number) => {
    const s = 0.0022 / zoom * (1 / Math.max(0.25, W / Math.max(1, init.w)));
    const speed = 60 * dt * Math.max(0.6, W / 1400) * 1.4;
    t += dt * 0.03;
    for (let i = 0; i < N; i++) {
      const [vx, vy] = curl2(noise, X[i] * s, Y[i] * s, t, 0.01);
      X[i] += vx * speed * 0.35; Y[i] += vy * speed * 0.35;
      if (--life[i] === 0 || X[i] < -2 || Y[i] < -2 || X[i] > W + 2 || Y[i] > H + 2) spawn(i);
    }
  };
  /** Fade the buffer and deposit each particle's path since the previous call (PX,PY -> X,Y). */
  const deposit = () => {
    for (let i = 0; i < I.length; i++) I[i] *= trail;
    for (let i = 0; i < N; i++) {
      const dx = X[i] - PX[i], dy = Y[i] - PY[i];
      const n = Math.ceil(Math.max(Math.abs(dx), Math.abs(dy)));
      if (n > 0 && n < 24) {
        for (let j = 1; j <= n; j++) {
          const x = (PX[i] + dx * j / n) | 0, y = (PY[i] + dy * j / n) | 0;
          if (x >= 0 && y >= 0 && x < W && y < H) I[y * W + x] += 0.5;
        }
      }
      PX[i] = X[i]; PY[i] = Y[i];
    }
    dirty = true;
  };
  const show = () => {
    if (!img || !dirty) return;
    dirty = false;
    const d = img.data, gain = 0.35 + 0.9 * k;
    for (let i = 0, j = 0; i < I.length; i++, j += 4) {
      const v = I[i] > 0.002 ? ((1 - Math.exp(-I[i] * gain)) * 255) | 0 : 0;
      d[j] = lut[v * 3]; d[j + 1] = lut[v * 3 + 1]; d[j + 2] = lut[v * 3 + 2]; d[j + 3] = 255;
    }
    ctx.putImageData(img, 0, 0);
  };
  return {
    resize(w, h, cap) {
      fitCanvas(init.canvas, w, h, Math.min(0.5, cap));
      if (init.canvas.width * init.canvas.height !== I.length || !img) {
        W = init.canvas.width; H = init.canvas.height;
        I = new Float32Array(W * H); img = ctx.createImageData(W, H);
        for (let i = 0; i < N; i++) spawn(i);
      }
      dirty = true;
    },
    setPalette(np, intensity) {
      pal = np; k = intensity;
      for (let i = 0; i < 256; i++) {
        const c = ramp(pal, Math.min(1, (i / 255) * 0.92));
        lut[i * 3] = c[0] * 255; lut[i * 3 + 1] = c[1] * 255; lut[i * 3 + 2] = c[2] * 255;
      }
      dirty = true;
    },
    step(dt) { advance(dt); },
    draw() { deposit(); show(); },
    settle() {
      I.fill(0);
      for (let f = 0; f < 90; f++) { advance(1 / 30); deposit(); }
      show();
    },
    dispose() { I = new Float32Array(1); img = null; },
  };
}
