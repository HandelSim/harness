import { test } from 'node:test';
import assert from 'node:assert/strict';
import { p95, isJanky, JankMeter, type JankSummary } from './jank';
import { Governor, LADDER } from './governor';

test('jank: p95 nearest rank, ignores junk', () => {
  assert.equal(p95([]), 0);
  assert.equal(p95(Array.from({ length: 100 }, (_, i) => i + 1)), 95);
  assert.equal(p95([16.7, NaN, -1, Infinity, 16.7]), 16.7);
  assert.equal(p95([...Array(19).fill(16), 100]), 16);
  assert.equal(p95([...Array(18).fill(16), 100, 100]), 100);
});
test('jank: policy', () => {
  const ok: JankSummary = { p95: 17, longMs: 0, windowMs: 2000, n: 120 };
  assert.equal(isJanky(ok), false);
  assert.equal(isJanky({ ...ok, p95: 50 }), true);
  assert.equal(isJanky({ ...ok, p95: 33.4 }), false, 'two vsyncs is not jank');
  assert.equal(isJanky({ ...ok, longMs: 600 }), true);
  assert.equal(isJanky({ ...ok, longMs: 400 }), false);
  assert.equal(isJanky({ ...ok, p95: 500, n: 5 }), false, 'too few samples');
  assert.equal(isJanky({ ...ok, windowMs: 0, longMs: 10 }), false);
});
test('jank: meter reports about once a second over a 2 s window', () => {
  const reports: JankSummary[] = [];
  const m = new JankMeter({ raf: () => 1, caf: () => {}, now: () => 0, onReport: (s) => reports.push(s) });
  m.start();
  for (let t = 0; t <= 5000; t += 1000 / 60) m.frame(t);
  assert.ok(reports.length >= 3 && reports.length <= 4, String(reports.length));
  const last = reports.at(-1)!;
  assert.ok(Math.abs(last.p95 - 16.7) < 0.2, String(last.p95));
  assert.ok(last.n >= 115 && last.n <= 121, String(last.n));
  assert.equal(last.windowMs, 2000);
  assert.equal(isJanky(last), false);
});
test('jank: slow frames and long tasks show up; reset drops the window; stop ignores long tasks', () => {
  const reports: JankSummary[] = [];
  const m = new JankMeter({ raf: () => 1, caf: () => {}, now: () => 0, onReport: (s) => reports.push(s) });
  m.start();
  let t = 0;
  for (; t <= 3000; t += 60) m.frame(t);
  assert.ok(isJanky(reports.at(-1)!), JSON.stringify(reports.at(-1)));
  m.reset(); reports.length = 0;
  for (const end = t + 3000; t <= end; t += 1000 / 60) { m.frame(t); if (Math.round(t) % 100 < 17) m.longTask(t, 60); }
  const r = reports.at(-1)!;
  assert.ok(r.p95 < 20 && r.longMs > 500 && isJanky(r), JSON.stringify(r));
  m.stop();
  m.longTask(t, 1000);
  m.start(); reports.length = 0;
  for (const end = t + 2000; t <= end; t += 1000 / 60) m.frame(t);
  assert.equal(reports.at(-1)!.longMs, 0);
});
test('governor: sustained jank steps exactly one rung; spikes and pause reset it', () => {
  const g = new Governor({ budgetMs: 4 });
  assert.equal(g.jank(true, 0), false);
  assert.equal(g.jank(true, 1000), false);
  assert.equal(g.jank(false, 1500), false, 'a clean report clears the hold');
  assert.equal(g.jank(true, 2000), false);
  assert.equal(g.jank(true, 3000), false);
  assert.equal(g.jank(true, 4000, 'main thread frame p95 50 ms'), true);
  assert.equal(g.level, 1);
  assert.match(g.reason, /jank/);
  assert.equal(g.jank(true, 5000), false, 'a downgrade restarts the hold');
  g.pause();
  assert.equal(g.jank(true, 6000), false);
  assert.equal(g.jank(true, 7000), false);
  assert.equal(g.jank(true, 8000), true);
  assert.equal(g.level, 2);
  g.reset();
  assert.equal(g.level, 0);
  assert.equal(g.jank(true, 9000), false, 'reset clears the hold');
});
test('governor: jank walks to static and stops; never climbs back', () => {
  const g = new Governor({ budgetMs: 4 });
  let steps = 0;
  for (let t = 0; t < 120_000; t += 1000) if (g.jank(true, t)) steps++;
  assert.equal(steps, LADDER.length - 1);
  assert.equal(g.rung.static, true);
  for (let t = 120_000; t < 130_000; t += 1000) g.jank(false, t);
  assert.equal(g.rung.static, true);
});

// review2 P1-4: the threshold follows the display's frame time; the worker rung ignores long tasks.
test('jank: threshold is relative to the display frame time', async () => {
  const { jankThreshold } = await import('./jank');
  assert.equal(jankThreshold(), 34);
  assert.equal(jankThreshold(16.7), 37.4);
  assert.ok(Math.abs(jankThreshold(33.3) - 70.6) < 1e-9);
  assert.equal(jankThreshold(60), 34, 'slower than any display: it is the scene, so 60 Hz rules');
  assert.equal(jankThreshold(NaN), 34);
  const base: JankSummary = { p95: 35, longMs: 0, windowMs: 2000, n: 60 };
  assert.equal(isJanky(base), true, 'no estimate: 60 Hz rules');
  assert.equal(isJanky({ ...base, vsyncMs: 33.3 }), false, '30 Hz display: 35 ms is ordinary jitter');
  assert.equal(isJanky({ ...base, p95: 80, vsyncMs: 33.3 }), true);
});
test('jank: a 30 Hz display never reads as janky; its estimate survives reset()', () => {
  const reports: JankSummary[] = [];
  const m = new JankMeter({ raf: () => 1, caf: () => {}, now: () => 0, onReport: (s) => reports.push(s) });
  m.start();
  let t = 0, i = 0;
  for (; t <= 14_000; i++) { m.frame(t); t += 1000 / 30 + (i % 7 === 0 ? 2.5 : (i % 3) * 0.4); }
  assert.ok(reports.length > 5);
  for (const r of reports) assert.equal(isJanky(r), false, JSON.stringify(r));
  assert.ok(Math.abs(reports.at(-1)!.vsyncMs! - 33.3) < 1, String(reports.at(-1)!.vsyncMs));
  m.reset(); reports.length = 0;
  for (const end = t + 2000; t <= end; t += 1000 / 30) m.frame(t);
  assert.ok(reports.at(-1)!.vsyncMs! < 34.5, 'estimate kept across a reset');
});
test('jank: worker summary drops long tasks and the frames that overlap them', async () => {
  const { workerJank } = await import('./jank');
  const reports: JankSummary[] = [];
  const m = new JankMeter({ raf: () => 1, caf: () => {}, now: () => 0, onReport: (s) => reports.push(s) });
  m.start();
  let t = 0, i = 0;
  // 60 Hz with a 120 ms app long task every ~300 ms (e.g. a big diff render): main-thread p95 is the long frame
  while (t <= 4000) {
    m.frame(t);
    if (i % 12 === 11) { m.longTask(t + 1, 120); t += 121; } else t += 1000 / 60;
    i++;
  }
  const r = reports.at(-1)!;
  assert.equal(isJanky(r), true, 'main2d still sees it: ' + JSON.stringify(r));
  const w = workerJank(r);
  assert.equal(w.longMs, 0);
  assert.ok(w.p95 < 20, JSON.stringify(w));
  assert.equal(isJanky(w), false, 'the worker scene is not blamed for app work');
});
