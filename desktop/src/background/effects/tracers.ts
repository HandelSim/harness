// B7: particles integrated with RK4 on a 3D attractor, slow yaw, additive short trails.
import type { Effect, Ctx2D, EffectInit } from './types';
import { fitCanvas } from './types';
import { type Palette, toCss, mix } from '../palette';
import { mulberry32, deriveSeed } from '../prng';
import { SYSTEMS, rk4, type Vec3 } from './attractor';

export function makeTracers2d(ctx: Ctx2D, init: EffectInit): Effect {
  const sys = SYSTEMS[String(init.cfg.params.system)] ?? SYSTEMS.lorenz;
  const N = Math.max(10, Math.min(2000, Number(init.cfg.params.count) || 600));
  const rng = mulberry32(deriveSeed(init.cfg.seed, 'tracers'));
  // Probe trajectory: settles onto the attractor, gives the fit box and spawn points.
  let s: Vec3 = [sys.start[0] + rng() * 0.01, sys.start[1], sys.start[2]];
  for (let i = 0; i < 2000; i++) s = rk4(sys.f, s, sys.dt);
  const pts: Vec3[] = [];
  const lo: Vec3 = [Infinity, Infinity, Infinity], hi: Vec3 = [-Infinity, -Infinity, -Infinity];
  for (let i = 0; i < 6000; i++) {
    s = rk4(sys.f, s, sys.dt);
    if (i % 3 === 0) pts.push(s);
    for (let k = 0; k < 3; k++) { lo[k] = Math.min(lo[k], s[k]); hi[k] = Math.max(hi[k], s[k]); }
  }
  const c: Vec3 = [(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2];
  const ext = Math.max(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]) / 2 || 1;
  const P = new Float64Array(N * 3), prev = new Float32Array(N * 2), age = new Uint16Array(N);
  const spawn = (i: number) => {
    const q = pts[(rng() * pts.length) | 0];
    P[i * 3] = q[0] + (rng() - 0.5) * ext * 0.02; P[i * 3 + 1] = q[1] + (rng() - 0.5) * ext * 0.02; P[i * 3 + 2] = q[2] + (rng() - 0.5) * ext * 0.02;
    age[i] = 0; prev[i * 2] = NaN;
  };
  for (let i = 0; i < N; i++) spawn(i);
  let yaw = rng() * Math.PI * 2, W = 1, H = 1, scale = 1;
  let pal: Palette = init.pal, k = 0.5, colA = '', colB = '', fade = '';
  // Lorenz/Halvorsen look best viewed with their long axis vertical-ish: rotate so axis 2 is up.
  const up = 2;
  const project = (i: number, out: Float32Array, o: number): number => {
    const x = (P[i * 3] - c[0]) / ext, y = (P[i * 3 + 1] - c[1]) / ext, z = (P[i * 3 + 2] - c[2]) / ext;
    const [a, b, v] = up === 2 ? [x, y, z] : [x, z, y];
    const cs = Math.cos(yaw), sn = Math.sin(yaw);
    const rx = a * cs - b * sn, depth = a * sn + b * cs;
    const persp = 1 / (2.6 - depth * 0.6);
    out[o] = W / 2 + rx * scale * persp * 2.2; out[o + 1] = H / 2 - v * scale * persp * 2.2;
    return depth;
  };
  const cur = new Float32Array(2);
  const stepAll = (h: number) => {
    for (let i = 0; i < N; i++) {
      const q = rk4(sys.f, [P[i * 3], P[i * 3 + 1], P[i * 3 + 2]], h);
      if (!Number.isFinite(q[0] + q[1] + q[2]) || Math.abs(q[0] - c[0]) > ext * 4 || Math.abs(q[1] - c[1]) > ext * 4 || Math.abs(q[2] - c[2]) > ext * 4 || ++age[i] > 4000 + (i % 997)) { spawn(i); continue; }
      P[i * 3] = q[0]; P[i * 3 + 1] = q[1]; P[i * 3 + 2] = q[2];
    }
  };
  return {
    resize(w, h, cap) {
      fitCanvas(init.canvas, w, h, Math.min(0.5, cap));
      W = init.canvas.width; H = init.canvas.height; scale = Math.min(W, H) * 0.42;
      ctx.fillStyle = toCss(pal.c0); ctx.fillRect(0, 0, W, H);
      prev.fill(NaN);
    },
    setPalette(np, intensity) {
      pal = np; k = intensity;
      const a = 0.05 + 0.25 * k;
      colA = toCss(mix(pal.c2, pal.c3, 0.2), a); colB = toCss(pal.c3, a * 0.8);
      fade = toCss(pal.c0, 0.08);
    },
    step(dt) {
      yaw += dt * 0.05;
      stepAll(sys.dt * Math.min(3, dt * 60) * 0.6);
    },
    draw() {
      ctx.globalCompositeOperation = 'source-over';
      ctx.fillStyle = fade; ctx.fillRect(0, 0, W, H);
      ctx.globalCompositeOperation = pal.dark ? 'lighter' : 'source-over';
      ctx.lineWidth = Math.max(0.6, W / 900);
      for (let pass = 0; pass < 2; pass++) {
        ctx.strokeStyle = pass === 0 ? colA : colB;
        ctx.beginPath();
        for (let i = pass; i < N; i += 2) {
          project(i, cur, 0);
          const px = prev[i * 2], py = prev[i * 2 + 1];
          if (px === px) { ctx.moveTo(px, py); ctx.lineTo(cur[0], cur[1]); }
          prev[i * 2] = cur[0]; prev[i * 2 + 1] = cur[1];
        }
        ctx.stroke();
      }
      ctx.globalCompositeOperation = 'source-over';
    },
    settle() {
      ctx.fillStyle = toCss(pal.c0); ctx.fillRect(0, 0, W, H);
      prev.fill(NaN);
      for (let f = 0; f < 90; f++) { stepAll(sys.dt * 0.6); this.draw(); }
    },
    dispose() {},
  };
}
