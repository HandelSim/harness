import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mapStep, mapBounds, SYSTEMS, rk4, type Vec3 } from './effects/attractor';
import { CLIFFORD_PRESETS, DEJONG_PRESETS } from './catalog';

test('attractor: every Clifford and de Jong preset stays finite and inside its bound for 1e5 steps', () => {
  for (const [kind, sets] of [['clifford', CLIFFORD_PRESETS], ['dejong', DEJONG_PRESETS]] as const) {
    for (const p of sets) {
      const [bx, by] = mapBounds(kind, p);
      let x = 0.1, y = 0.1;
      for (let i = 0; i < 1e5; i++) {
        [x, y] = mapStep(kind, p, x, y);
        assert.ok(Number.isFinite(x) && Number.isFinite(y));
        assert.ok(Math.abs(x) <= bx + 1e-9 && Math.abs(y) <= by + 1e-9, `${kind} ${p} escaped at ${i}`);
      }
    }
  }
});
test('attractor: Lorenz, Aizawa, Thomas, Halvorsen stay finite and bounded for 1e5 RK4 steps', () => {
  const box: Record<string, number> = { lorenz: 60, aizawa: 3, thomas: 6, halvorsen: 20 };
  for (const [name, sys] of Object.entries(SYSTEMS)) {
    let s: Vec3 = [...sys.start];
    let maxAbs = 0;
    for (let i = 0; i < 1e5; i++) {
      s = rk4(sys.f, s, sys.dt);
      assert.ok(s.every(Number.isFinite), `${name} not finite at ${i}`);
      maxAbs = Math.max(maxAbs, ...s.map(Math.abs));
    }
    assert.ok(maxAbs < box[name], `${name} max |coord| ${maxAbs}`);
    assert.ok(maxAbs > box[name] / 20, `${name} collapsed to a point (${maxAbs})`);
  }
});
test('attractor: one RK4 Lorenz step matches the hand-checked value', () => {
  // From (1,1,1), h = 0.01, sigma 10, rho 28, beta 8/3:
  // k1 = (0, 26, -5/3); s + h/2 k1 = (1, 1.13, 0.991667) -> k2 = (1.3, 25.878333, -1.514444);
  // k3 = (1.228917, 26.053730, -1.509741); k4 = (2.482481, 26.086553, -1.350379)
  // next = s + h/6 (k1 + 2k2 + 2k3 + k4)
  const next = rk4(SYSTEMS.lorenz.f, [1, 1, 1], 0.01);
  const want = [1.0125671910736112, 1.2599177989452743, 0.9848909717916053];
  next.forEach((v, i) => assert.ok(Math.abs(v - want[i]) < 1e-12, `${i}: ${v} vs ${want[i]}`));
});

test('attractor: every preset is chaotic; morph waypoint paths are mostly chaotic', async () => {
  const { spreadOk, pickWaypoint } = await import('./effects/attractor');
  const { mulberry32 } = await import('./prng');
  for (const [kind, sets] of [['clifford', CLIFFORD_PRESETS], ['dejong', DEJONG_PRESETS]] as const) {
    sets.forEach((base, i) => {
      assert.ok(spreadOk(kind, base), `${kind} ${i} collapsed`);
      const rng = mulberry32(i + 1);
      let from: number[] = base.slice(), bad = 0;
      for (let leg = 0; leg < 3; leg++) {
        const to = pickWaypoint(kind, base, from, rng);
        for (let s = 0; s <= 100; s++) if (!spreadOk(kind, from.map((v, j) => v + (to[j] - v) * (s / 100)))) bad++;
        from = to;
      }
      assert.ok(bad <= 15, `${kind} ${i}: ${bad}/303 path samples collapsed`);
    });
  }
});

test('attractor effect: a 60 s morph never shows an empty frame', async () => {
  const { makeAttractor2d } = await import('./effects/attractor');
  const { derivePalette } = await import('./palette');
  const { DEFAULT_BG, defaultParams, descriptor } = await import('./catalog');
  for (const [map, preset] of [['clifford', '0'], ['clifford', '1'], ['dejong', '0'], ['dejong', '6']]) {
    const canvas = { width: 1, height: 1 } as unknown as OffscreenCanvas;
    let lastData: Uint8ClampedArray | null = null;
    const ctx = {
      canvas,
      createImageData: (w: number, h: number) => ({ width: w, height: h, data: new Uint8ClampedArray(w * h * 4) }),
      putImageData: (img: { data: Uint8ClampedArray }) => { lastData = img.data; },
    } as unknown as OffscreenCanvasRenderingContext2D;
    const pal = derivePalette(null);
    const cfg = { ...DEFAULT_BG, id: 'attractor', params: { ...defaultParams(descriptor('attractor')!), map, preset } };
    const e = makeAttractor2d(ctx, { canvas, cfg, pal, w: 240, h: 160 });
    e.resize(240, 160, 1);
    e.setPalette(pal, 0.5);
    let minLit = Infinity;
    for (let f = 0; f < 60 * 15; f++) {           // 15 fps, 4 fixed steps per frame
      for (let k = 0; k < 4; k++) e.step(1 / 60);
      e.draw();
      if (f < 30 || !lastData) continue;
      const d: Uint8ClampedArray = lastData;
      let lit = 0;
      for (let j = 0; j < d.length; j += 4) if (d[j] + d[j + 1] + d[j + 2] > 3 * 255 * pal.c0[0] + 30) lit++;
      minLit = Math.min(minLit, lit);
    }
    assert.ok(minLit > 200, `${map} ${preset}: a frame had only ${minLit} lit pixels`);
  }
});
