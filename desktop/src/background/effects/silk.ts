// B3 silk: Inigo Quilez domain-warped fBm. WebGL2 at low resolution; CPU fallback at very low resolution.
import type { Effect, Ctx2D, EffectInit } from './types';
import { fitCanvas } from './types';
import { type Palette, mix, type RGB } from '../palette';
import { mulberry32, deriveSeed } from '../prng';
import { valueNoise2, fbm2 } from '../noise';
import { createProgram, fullscreenTriangle, FS_HEADER, VS_FULLSCREEN, GLSL_NOISE, setPaletteUniforms } from '../gl/program';

const FS = `${FS_HEADER}
uniform vec2 u_res; uniform float u_time, u_scale, u_warp, u_k;
uniform vec3 u_c0, u_c1, u_c2, u_c3;
${GLSL_NOISE}
void main() {
  vec2 p = (gl_FragCoord.xy - 0.5 * u_res) / min(u_res.x, u_res.y) * 2.2 * u_scale;
  float t = u_time;
  vec2 q = vec2(fbm(p + vec2(0.0, 0.0) + 0.02 * t), fbm(p + vec2(5.2, 1.3)));
  vec2 r = vec2(fbm(p + u_warp * q + vec2(1.7, 9.2) + 0.15 * t), fbm(p + u_warp * q + vec2(8.3, 2.8) + 0.126 * t));
  float v = fbm(p + u_warp * r);
  vec3 col = mix(u_c0, u_c1, clamp(v * v * 3.2, 0., 1.));
  col = mix(col, u_c2, clamp(dot(q, q) * 0.55, 0., 1.) * 0.85);
  col = mix(col, u_c3, clamp(r.x * r.x * r.x, 0., 1.) * 0.45);
  col = mix(u_c0, col, clamp(u_k * 1.5, 0., 1.));
  col += (hash(gl_FragCoord.xy + fract(t)) - 0.5) / 255.0;
  o = vec4(col, 1.0);
}`;

const params = (init: EffectInit) => ({
  scale: Number(init.cfg.params.scale) || 1.4,
  warp: Number(init.cfg.params.warp) || 4,
  seed: mulberry32(deriveSeed(init.cfg.seed, 'silk'))(),
});

export function makeSilkGL(gl: WebGL2RenderingContext, init: EffectInit): Effect {
  const prm = params(init);
  const prog = createProgram(gl, VS_FULLSCREEN, FS), tri = fullscreenTriangle(gl);
  let t = prm.seed * 100, pal: Palette = init.pal, k = 0.5;
  return {
    resize(w, h, cap) { fitCanvas(init.canvas, w, h, Math.min(0.35, cap)); },
    setPalette(np, intensity) { pal = np; k = intensity; },
    step(dt) { t += dt; },
    draw() {
      gl.viewport(0, 0, init.canvas.width, init.canvas.height);
      gl.useProgram(prog.prog);
      gl.uniform2f(prog.u('u_res'), init.canvas.width, init.canvas.height);
      gl.uniform1f(prog.u('u_time'), t); gl.uniform1f(prog.u('u_scale'), prm.scale); gl.uniform1f(prog.u('u_warp'), prm.warp);
      gl.uniform1f(prog.u('u_k'), k); gl.uniform1f(prog.u('u_seed'), prm.seed);
      setPaletteUniforms(gl, prog, pal);
      tri.draw();
    },
    settle() { /* stateless in t: any frame is settled */ },
    dispose() { prog.dispose(); tri.dispose(); },
  };
}

export function makeSilk2d(ctx: Ctx2D, init: EffectInit): Effect {
  const prm = params(init);
  const n = valueNoise2(deriveSeed(init.cfg.seed, 'silk-cpu'));
  let t = prm.seed * 100, pal: Palette = init.pal, k = 0.5, img: ImageData | null = null;
  const c = (x: number) => (x < 0 ? 0 : x > 1 ? 1 : x);
  return {
    resize(w, h, cap) {
      fitCanvas(init.canvas, w, h, Math.min(0.06, cap * 0.2));
      img = ctx.createImageData(init.canvas.width, init.canvas.height);
    },
    setPalette(np, intensity) { pal = np; k = intensity; },
    step(dt) { t += dt; },
    draw() {
      if (!img) return;
      const W = img.width, H = img.height, d = img.data, m = Math.min(W, H);
      const kk = c(k * 1.5);
      for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
        const px = (x - W / 2) / m * 2.2 * prm.scale, py = (H / 2 - y) / m * 2.2 * prm.scale;
        const qx = fbm2(n, px + 0.02 * t, py, 4), qy = fbm2(n, px + 5.2, py + 1.3, 4);
        const rx = fbm2(n, px + prm.warp * qx + 1.7 + 0.15 * t, py + prm.warp * qy + 9.2, 4);
        const ry = fbm2(n, px + prm.warp * qx + 8.3 + 0.126 * t, py + prm.warp * qy + 2.8, 4);
        const v = fbm2(n, px + prm.warp * rx, py + prm.warp * ry, 4);
        let col: RGB = mix(pal.c0, pal.c1, c(v * v * 3.2));
        col = mix(col, pal.c2, c((qx * qx + qy * qy) * 0.55) * 0.85);
        col = mix(col, pal.c3, c(rx * rx * rx) * 0.45);
        col = mix(pal.c0, col, kk);
        const j = (y * W + x) * 4;
        d[j] = col[0] * 255; d[j + 1] = col[1] * 255; d[j + 2] = col[2] * 255; d[j + 3] = 255;
      }
      ctx.putImageData(img, 0, 0);
    },
    settle() {},
    dispose() { img = null; },
  };
}
