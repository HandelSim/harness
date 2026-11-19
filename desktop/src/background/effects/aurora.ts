// B4 aurora: layered Gaussian bands warped by fBm, with vertical ray texture, exp tone map.
import type { Effect, EffectInit } from './types';
import { fitCanvas } from './types';
import type { Palette } from '../palette';
import { mulberry32, deriveSeed } from '../prng';
import { createProgram, fullscreenTriangle, FS_HEADER, VS_FULLSCREEN, GLSL_NOISE, setPaletteUniforms } from '../gl/program';

const FS = `${FS_HEADER}
uniform vec2 u_res; uniform float u_time, u_height, u_k; uniform int u_bands;
uniform vec3 u_c0, u_c1, u_c2, u_c3;
${GLSL_NOISE}
void main() {
  vec2 uv = gl_FragCoord.xy / u_res;
  float aspect = u_res.x / u_res.y;
  float y = 1.0 - uv.y;                         // 0 at the top
  float t = u_time;
  vec3 acc = vec3(0.);
  for (int k = 0; k < 3; k++) {
    if (k >= u_bands) break;
    float fk = float(k);
    float freq = 1.5 + fk * 1.1;
    float x = uv.x * aspect * freq + fbm(vec2(uv.x * aspect * 0.7 + t * 0.05 * (fk + 1.), fk * 3.1)) * 1.6;
    float center = u_height + fk * 0.07 + 0.08 * sin(x * 2. + t * 0.1 * (fk + 1.));
    float w = 0.05 + 0.035 * fk;
    float d = (y - center) / w;
    float band = exp(-d * d);
    float above = smoothstep(center + w, center - 0.28, y) * step(y, center);   // curtain tail toward the top
    // two low octaves only: full fBm at x*6 aliases into hard stripes at a 0.25 render scale
    float rays = 0.6 * vnoise(vec2(x * 2.5, t * 0.15 + fk * 7.)) + 0.4 * vnoise(vec2(x * 5., t * 0.2 + fk * 3.));
    float a = (band + above * 0.55) * (0.35 + 0.9 * rays * rays);
    vec3 col = k == 0 ? u_c2 : k == 1 ? mix(u_c2, u_c3, 0.5) : u_c1 * 1.6;
    acc += col * a * (0.8 - fk * 0.18);
  }
  vec3 tone = 1.0 - exp(-acc * 1.6);
  vec3 col = u_c0 + tone * clamp(u_k * 1.4, 0., 1.);
  col += (hash(gl_FragCoord.xy + fract(t)) - 0.5) / 255.0;
  o = vec4(col, 1.0);
}`;

export function makeAuroraGL(gl: WebGL2RenderingContext, init: EffectInit): Effect {
  const height = Number(init.cfg.params.height) || 0.3;
  const bands = Math.max(1, Math.min(3, Math.round(Number(init.cfg.params.bands) || 3)));
  const seed = mulberry32(deriveSeed(init.cfg.seed, 'aurora'))();
  const prog = createProgram(gl, VS_FULLSCREEN, FS), tri = fullscreenTriangle(gl);
  let t = seed * 100, pal: Palette = init.pal, k = 0.5;
  return {
    resize(w, h, cap) { fitCanvas(init.canvas, w, h, Math.min(0.25, cap)); },
    setPalette(np, intensity) { pal = np; k = intensity; },
    step(dt) { t += dt; },
    draw() {
      gl.viewport(0, 0, init.canvas.width, init.canvas.height);
      gl.useProgram(prog.prog);
      gl.uniform2f(prog.u('u_res'), init.canvas.width, init.canvas.height);
      gl.uniform1f(prog.u('u_time'), t); gl.uniform1f(prog.u('u_height'), height); gl.uniform1i(prog.u('u_bands'), bands);
      gl.uniform1f(prog.u('u_k'), k); gl.uniform1f(prog.u('u_seed'), seed);
      setPaletteUniforms(gl, prog, pal);
      tri.draw();
    },
    settle() {},
    dispose() { prog.dispose(); tri.dispose(); },
  };
}
