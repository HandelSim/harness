// Minimal WebGL2 helpers: compile/link, fullscreen triangle, palette uniforms.
import type { Palette } from '../palette';

export const VS_FULLSCREEN = `#version 300 es
in vec2 a_pos;
void main() { gl_Position = vec4(a_pos, 0.0, 1.0); }`;
export const FS_HEADER = `#version 300 es
precision highp float;
out vec4 o;`;

export interface Program { prog: WebGLProgram; u(name: string): WebGLUniformLocation | null; dispose(): void }

export function createProgram(gl: WebGL2RenderingContext, vs: string, fs: string): Program {
  const sh = (type: number, src: string) => {
    const s = gl.createShader(type);
    if (!s) throw new Error('webgl2: createShader failed');
    gl.shaderSource(s, src); gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS) && !gl.isContextLost()) {
      const log = gl.getShaderInfoLog(s); gl.deleteShader(s);
      throw new Error(`webgl2 shader: ${log}`);
    }
    return s;
  };
  const v = sh(gl.VERTEX_SHADER, vs), f = sh(gl.FRAGMENT_SHADER, fs);
  const prog = gl.createProgram();
  if (!prog) throw new Error('webgl2: createProgram failed');
  gl.attachShader(prog, v); gl.attachShader(prog, f);
  gl.bindAttribLocation(prog, 0, 'a_pos');
  gl.linkProgram(prog);
  gl.deleteShader(v); gl.deleteShader(f);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS) && !gl.isContextLost()) throw new Error(`webgl2 link: ${gl.getProgramInfoLog(prog)}`);
  const cache = new Map<string, WebGLUniformLocation | null>();
  return {
    prog,
    u(name) { if (!cache.has(name)) cache.set(name, gl.getUniformLocation(prog, name)); return cache.get(name)!; },
    dispose() { gl.deleteProgram(prog); },
  };
}

export function fullscreenTriangle(gl: WebGL2RenderingContext): { draw(): void; dispose(): void } {
  const vao = gl.createVertexArray(), buf = gl.createBuffer();
  gl.bindVertexArray(vao);
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
  gl.enableVertexAttribArray(0);
  gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
  gl.bindVertexArray(null);
  return {
    draw() { gl.bindVertexArray(vao); gl.drawArrays(gl.TRIANGLES, 0, 3); gl.bindVertexArray(null); },
    dispose() { gl.deleteBuffer(buf); gl.deleteVertexArray(vao); },
  };
}

export function setPaletteUniforms(gl: WebGL2RenderingContext, p: Program, pal: Palette): void {
  gl.uniform3fv(p.u('u_c0'), pal.c0); gl.uniform3fv(p.u('u_c1'), pal.c1);
  gl.uniform3fv(p.u('u_c2'), pal.c2); gl.uniform3fv(p.u('u_c3'), pal.c3);
}

/** GLSL value noise + fBm (shared by silk and aurora). */
export const GLSL_NOISE = `
uniform float u_seed;
float hash(vec2 p) { p = fract(p * vec2(123.34, 456.21) + u_seed); p += dot(p, p + 45.32); return fract(p.x * p.y); }
float vnoise(vec2 p) {
  vec2 i = floor(p), f = fract(p);
  vec2 u = f * f * (3. - 2. * f);
  return mix(mix(hash(i), hash(i + vec2(1, 0)), u.x), mix(hash(i + vec2(0, 1)), hash(i + vec2(1, 1)), u.x), u.y);
}
float fbm(vec2 p) {
  float s = 0., a = 0.5;
  mat2 r = mat2(0.8, 0.6, -0.6, 0.8);
  for (int i = 0; i < 5; i++) { s += a * vnoise(p); p = r * p * 2.0 + 17.3; a *= 0.5; }
  return s / 0.96875;
}
vec3 rampc(float x) { x = clamp(x, 0., 1.) * 3.; return x < 1. ? mix(u_c0, u_c1, x) : x < 2. ? mix(u_c1, u_c2, x - 1.) : mix(u_c2, u_c3, x - 2.); }
`;
