import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseToWorker, parseFromWorker, isConfig, isTheme } from './protocol';
import { DEFAULT_BG } from './catalog';

const theme = { mode: 'dark', base: '#000', fg: '#fff', accent: '#00f', accent2: '#0ff' };

test('protocol: valid control messages pass', () => {
  assert.ok(parseToWorker({ t: 'config', id: 1, config: DEFAULT_BG }));
  assert.ok(parseToWorker({ t: 'resize', id: 1, w: 800, h: 600 }));
  assert.ok(parseToWorker({ t: 'drop', id: 3 }));
  assert.ok(parseToWorker({ t: 'theme', theme }));
  assert.ok(parseToWorker({ t: 'state', state: 'dim', reduced: false }));
  assert.ok(parseToWorker({ t: 'thumb', req: 1, config: DEFAULT_BG, w: 96, h: 60, theme }));
  assert.ok(parseToWorker({ t: 'debug', burnMs: 10 }));
  assert.ok(parseToWorker({ t: 'jank', id: 2, p95: 33.4, longMs: 0, windowMs: 2000, n: 120 }));
});
test('protocol: malformed messages are rejected', () => {
  const bad: unknown[] = [
    null, 1, 'x', [], {}, { t: 'nope' },
    { t: 'config', id: -1, config: DEFAULT_BG }, { t: 'config', id: 1, config: { ...DEFAULT_BG, fpsCap: 45 } },
    { t: 'config', id: 1, config: { ...DEFAULT_BG, params: { a: {} } } },
    { t: 'resize', id: 1, w: 0, h: 600 }, { t: 'resize', id: 1, w: 1e9, h: 600 }, { t: 'resize', id: 1.5, w: 10, h: 10 },
    { t: 'theme', theme: { ...theme, mode: 'sepia' } }, { t: 'theme', theme: { ...theme, base: 'x'.repeat(500) } },
    { t: 'state', state: 'stop', reduced: false }, { t: 'state', state: 'run' },
    { t: 'thumb', req: 1, config: DEFAULT_BG, w: 5000, h: 60, theme },
    { t: 'debug', burnMs: 1000 }, { t: 'debug', burnMs: -1 },
    { t: 'jank', id: 1, p95: NaN, longMs: 0, windowMs: 2000, n: 1 }, { t: 'jank', id: 1, p95: 20, longMs: -1, windowMs: 2000, n: 1 },
    { t: 'jank', id: 1, p95: 20, longMs: 0, windowMs: 0, n: 1 }, { t: 'jank', id: 1, p95: 20, longMs: 0, windowMs: 2000, n: 1.5 },
    { t: 'jank', id: -1, p95: 20, longMs: 0, windowMs: 2000, n: 1 }, { t: 'jank', id: 1, p95: '20', longMs: 0, windowMs: 2000, n: 1 },
    { t: 'jank', id: 1, p95: 20, longMs: 0, windowMs: 2000 },
    { t: 'init', id: 1, canvas: {}, w: 10, h: 10, config: DEFAULT_BG, allowGL: true },   // not an OffscreenCanvas
  ];
  for (const m of bad) assert.equal(parseToWorker(m), null, JSON.stringify(m));
});
test('protocol: worker -> main messages', () => {
  assert.ok(parseFromWorker({ t: 'ready', id: 1, renderer: 'webgl2' }));
  assert.ok(parseFromWorker({ t: 'stats', id: 1, fps: 30, frameMs: 2.1, renderer: 'worker2d', tier: 1, downgraded: true, reason: 'x', frames: 10 }));
  assert.ok(parseFromWorker({ t: 'fail', id: 1, reason: 'lost', contextLost: true }));
  assert.ok(parseFromWorker({ t: 'thumb', req: 2, blob: null, error: 'x' }));
  assert.equal(parseFromWorker({ t: 'ready', id: 1, renderer: 'main2d' }), null);
  assert.equal(parseFromWorker({ t: 'stats', id: 1, fps: NaN, frameMs: 1, renderer: 'webgl2', tier: 0, downgraded: false, reason: '', frames: 0 }), null);
  assert.equal(parseFromWorker({ t: 'stats', id: 1, fps: 1, frameMs: 1, renderer: 'webgl2', tier: 4, downgraded: false, reason: '', frames: 0 }), null);
  assert.equal(parseFromWorker({ t: 'thumb', req: 2, blob: 'data:', error: '' }), null);
});
test('protocol: isConfig / isTheme', () => {
  assert.ok(isConfig(DEFAULT_BG));
  assert.equal(isConfig({ ...DEFAULT_BG, seed: 'a' }), false);
  assert.ok(isTheme(theme));
  assert.equal(isTheme({ ...theme, accent: 3 }), false);
});
