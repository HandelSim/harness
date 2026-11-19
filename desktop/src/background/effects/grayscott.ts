// B8: Gray-Scott reaction-diffusion. CPU reference (tests + 2D fallback) and a
// WebGL2 ping-pong version.
import type { Effect, Ctx2D, EffectInit } from './types';
import { type Palette, ramp } from '../palette';
import { mulberry32, deriveSeed, type Rng } from '../prng';
import { GRAYSCOTT_PRESETS } from '../catalog';
import { createProgram, fullscreenTriangle, FS_HEADER, VS_FULLSCREEN, setPaletteUniforms } from '../gl/program';
import { createPingPong, type PingPong } from '../gl/fbo';

export const DA = 1.0, DB = 0.5;
export interface GSGrid { w: number; h: number; A: Float32Array; B: Float32Array; A2: Float32Array; B2: Float32Array }

export function gsCreate(w: number, h: number, rng: Rng, patches = 6): GSGrid {
  const g: GSGrid = { w, h, A: new Float32Array(w * h).fill(1), B: new Float32Array(w * h), A2: new Float32Array(w * h), B2: new Float32Array(w * h) };
  gsSeed(g, rng, patches);
  return g;
}
export function gsSeed(g: GSGrid, rng: Rng, patches: number): void {
  const r = Math.max(2, Math.round(Math.min(g.w, g.h) * 0.04));
  for (let p = 0; p < patches; p++) {
    const cx = (rng() * g.w) | 0, cy = (rng() * g.h) | 0;
    for (let y = -r; y <= r; y++) for (let x = -r; x <= r; x++) {
      if (x * x + y * y > r * r) continue;
      const i = ((cy + y + g.h) % g.h) * g.w + ((cx + x + g.w) % g.w);
      g.B[i] = 1; g.A[i] = 0.5;
    }
  }
}
/** One explicit Euler step, dt = 1, 3x3 Laplacian (centre -1, edges 0.2, diagonals 0.05), wrap. */
export function gsStep(g: GSGrid, F: number, K: number): void {
  const { w, h, A, B, A2, B2 } = g;
  for (let y = 0; y < h; y++) {
    const ym = ((y - 1 + h) % h) * w, y0 = y * w, yp = ((y + 1) % h) * w;
    for (let x = 0; x < w; x++) {
      const xm = (x - 1 + w) % w, xp = (x + 1) % w, i = y0 + x;
      const a = A[i], b = B[i];
      const la = 0.2 * (A[ym + x] + A[yp + x] + A[y0 + xm] + A[y0 + xp]) + 0.05 * (A[ym + xm] + A[ym + xp] + A[yp + xm] + A[yp + xp]) - a;
      const lb = 0.2 * (B[ym + x] + B[yp + x] + B[y0 + xm] + B[y0 + xp]) + 0.05 * (B[ym + xm] + B[ym + xp] + B[yp + xm] + B[yp + xp]) - b;
      const abb = a * b * b;
      let na = a + DA * la - abb + F * (1 - a), nb = b + DB * lb + abb - (K + F) * b;
      na = na < 0 ? 0 : na > 1 ? 1 : na; nb = nb < 0 ? 0 : nb > 1 ? 1 : nb;
      A2[i] = na; B2[i] = nb;
    }
  }
  g.A = A2; g.A2 = A; g.B = B2; g.B2 = B;
}

const preset = (init: EffectInit) => GRAYSCOTT_PRESETS[String(init.cfg.params.preset)] ?? GRAYSCOTT_PRESETS.coral;
const simSize = (w: number, h: number, target: number) => {
  const s = target / Math.max(w, h);
  return [Math.max(16, Math.round(w * s)), Math.max(16, Math.round(h * s))];
};

export function makeGrayScott2d(ctx: Ctx2D, init: EffectInit): Effect {
  const [F, K] = preset(init);
  const steps = Math.max(1, Math.min(16, Number(init.cfg.params.steps) || 8));
  const rng = mulberry32(deriveSeed(init.cfg.seed, 'grayscott'));
  let g: GSGrid | null = null, img: ImageData | null = null, pal: Palette = init.pal, k = 0.5, acc = 0, frames = 0;
  const lut = new Uint8ClampedArray(768);
  return {
    resize(w, h, cap) {
      const [sw, sh] = simSize(w, h, cap < 0.5 ? 96 : 140);
      if (g && g.w === sw && g.h === sh) return;
      init.canvas.width = sw; init.canvas.height = sh;
      g = gsCreate(sw, sh, rng, 8); img = ctx.createImageData(sw, sh);
    },
    setPalette(np, intensity) {
      pal = np; k = intensity;
      for (let i = 0; i < 256; i++) {
        const v = i / 255, s = v * v * (3 - 2 * v);
        const c = ramp(pal, s * (0.45 + 0.55 * k) * 0.85);
        lut[i * 3] = c[0] * 255; lut[i * 3 + 1] = c[1] * 255; lut[i * 3 + 2] = c[2] * 255;
      }
    },
    step(dt) {
      if (!g) return;
      acc += dt * 20;                                 // 20 sim frames per designed second
      while (acc >= 1) { for (let i = 0; i < steps; i++) gsStep(g, F, K); acc -= 1; if (++frames % 600 === 0) gsSeed(g, rng, 2); }
    },
    draw() {
      if (!g || !img) return;
      const d = img.data, B = g.B;
      for (let i = 0, j = 0; i < B.length; i++, j += 4) {
        const li = (Math.min(1, B[i] * 2.2) * 255 | 0) * 3;
        d[j] = lut[li]; d[j + 1] = lut[li + 1]; d[j + 2] = lut[li + 2]; d[j + 3] = 255;
      }
      ctx.putImageData(img, 0, 0);
    },
    settle() { if (g) for (let i = 0; i < 1500; i++) gsStep(g, F, K); },
    dispose() { g = null; img = null; },
  };
}

const FS_STEP = `${FS_HEADER}
uniform sampler2D u_state; uniform vec2 u_texel; uniform float u_F, u_K;
void main() {
  vec2 uv = gl_FragCoord.xy * u_texel;
  vec2 c = texture(u_state, uv).rg;
  vec2 l = -c
    + 0.2 * (texture(u_state, uv + vec2(u_texel.x, 0.)).rg + texture(u_state, uv - vec2(u_texel.x, 0.)).rg
           + texture(u_state, uv + vec2(0., u_texel.y)).rg + texture(u_state, uv - vec2(0., u_texel.y)).rg)
    + 0.05 * (texture(u_state, uv + u_texel).rg + texture(u_state, uv - u_texel).rg
            + texture(u_state, uv + vec2(u_texel.x, -u_texel.y)).rg + texture(u_state, uv + vec2(-u_texel.x, u_texel.y)).rg);
  float a = c.r, b = c.g, abb = a * b * b;
  float na = a + 1.0 * l.r - abb + u_F * (1. - a);
  float nb = b + 0.5 * l.g + abb - (u_K + u_F) * b;
  o = vec4(clamp(na, 0., 1.), clamp(nb, 0., 1.), 0., 1.);
}`;
const FS_SHOW = `${FS_HEADER}
uniform sampler2D u_state; uniform vec2 u_res;
uniform vec3 u_c0, u_c1, u_c2, u_c3; uniform float u_k;
vec3 rampc(float x) { x = clamp(x, 0., 1.) * 3.; return x < 1. ? mix(u_c0, u_c1, x) : x < 2. ? mix(u_c1, u_c2, x - 1.) : mix(u_c2, u_c3, x - 2.); }
void main() {
  float b = texture(u_state, gl_FragCoord.xy / u_res).g;
  float s = smoothstep(0.05, 0.45, b);
  o = vec4(rampc(s * (0.45 + 0.55 * u_k) * 0.85), 1.);
}`;

export function makeGrayScottGL(gl: WebGL2RenderingContext, init: EffectInit): Effect {
  if (!gl.getExtension('EXT_color_buffer_float')) throw new Error('webgl2: EXT_color_buffer_float missing');
  const [F, K] = preset(init);
  const steps = Math.max(1, Math.min(16, Number(init.cfg.params.steps) || 8));
  const rng = mulberry32(deriveSeed(init.cfg.seed, 'grayscott'));
  const stepP = createProgram(gl, VS_FULLSCREEN, FS_STEP), showP = createProgram(gl, VS_FULLSCREEN, FS_SHOW);
  const tri = fullscreenTriangle(gl);
  let pp: PingPong | null = null, sw = 0, sh = 0, acc = 0, pal: Palette = init.pal, k = 0.5, frames = 0;
  const upload = () => {
    const g = gsCreate(sw, sh, rng, 10);
    const data = new Float32Array(sw * sh * 2);
    for (let i = 0; i < sw * sh; i++) { data[i * 2] = g.A[i]; data[i * 2 + 1] = g.B[i]; }
    pp!.upload(data);
  };
  const sim = (n: number) => {
    if (!pp) return;
    gl.useProgram(stepP.prog);
    gl.uniform2f(stepP.u('u_texel'), 1 / sw, 1 / sh);
    gl.uniform1f(stepP.u('u_F'), F); gl.uniform1f(stepP.u('u_K'), K);
    gl.uniform1i(stepP.u('u_state'), 0);
    gl.viewport(0, 0, sw, sh);
    for (let i = 0; i < n; i++) {
      gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, pp.read);
      gl.bindFramebuffer(gl.FRAMEBUFFER, pp.writeFbo);
      tri.draw();
      pp.swap();
    }
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
  };
  return {
    resize(w, h, cap) {
      init.canvas.width = Math.max(1, Math.round(w * Math.min(0.5, cap))); init.canvas.height = Math.max(1, Math.round(h * Math.min(0.5, cap)));
      const [nw, nh] = simSize(w, h, cap < 0.5 ? 192 : 256);
      if (pp && nw === sw && nh === sh) return;
      sw = nw; sh = nh;
      pp?.dispose();
      pp = createPingPong(gl, sw, sh);
      upload();
    },
    setPalette(np, intensity) { pal = np; k = intensity; },
    step(dt) {
      acc += dt * 20;
      while (acc >= 1) { sim(steps); acc -= 1; if (++frames % 900 === 0) upload(); }
    },
    draw() {
      if (!pp) return;
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      gl.viewport(0, 0, init.canvas.width, init.canvas.height);
      gl.useProgram(showP.prog);
      gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, pp.read);
      gl.uniform1i(showP.u('u_state'), 0);
      gl.uniform2f(showP.u('u_res'), init.canvas.width, init.canvas.height);
      setPaletteUniforms(gl, showP, pal);
      gl.uniform1f(showP.u('u_k'), k);
      tri.draw();
    },
    settle() { for (let i = 0; i < 150; i++) sim(10); },
    dispose() { pp?.dispose(); pp = null; stepP.dispose(); showP.dispose(); tri.dispose(); },
  };
}
