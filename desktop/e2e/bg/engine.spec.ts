// L4 background engine e2e (PLAN 7.4): drives the dev-only bg-demo page (out/renderer/bg-demo.html), which exposes
// window.__bg = { engine, errors, stats, set } and mirrors the worker's cumulative frame count in #bg-root[data-bg-frames].
import { test, expect } from '../fixtures';
import type { Page } from '@playwright/test';
import { CATALOG } from '../../src/background/catalog';

type Probe = { frames: number; id: string | undefined; renderer: string | undefined; stats: { fps: number; renderer: string; tier: number; downgraded: boolean; reason: string } | null; errors: string[] };
const probe = (win: Page): Promise<Probe> => win.evaluate(() => {
  const r = document.getElementById('bg-root')!;
  const g = (window as unknown as { __bg: { stats: Probe['stats']; errors: string[] } }).__bg;
  return { frames: Number(r.dataset.bgFrames ?? 0), id: r.dataset.bgId, renderer: r.dataset.bgRenderer, stats: g.stats, errors: g.errors.slice() };
});
const set = (win: Page, c: Record<string, unknown>) => win.evaluate((cfg) => (window as unknown as { __bg: { set(c: unknown): Promise<void> } }).__bg.set(cfg), c);
const engine = (win: Page, fn: string, arg: unknown) => win.evaluate(([f, a]) => {
  const e = (window as unknown as { __bg: { engine: Record<string, (x: unknown) => unknown> } }).__bg.engine;
  return void e[f as string](a);
}, [fn, arg] as const);

async function openDemo(win: Page, q = 'thumbs=0'): Promise<string[]> {
  const pageErrors: string[] = [];
  win.on('pageerror', (e) => pageErrors.push(e.message));
  win.on('console', (m) => { if (m.type() === 'error') pageErrors.push(m.text()); });
  await win.goto(`app://harness/bg-demo.html?${q}`);
  await win.waitForFunction(() => location.pathname.endsWith('/bg-demo.html') && document.documentElement.dataset.ready === '1'
    && !!(window as unknown as { __bg?: unknown }).__bg, undefined, { timeout: 15000 });
  return pageErrors;
}

test.describe('background engine', () => {
  test('every catalog id: set resolves, stats arrive, no errors', async ({ win }) => {
    test.setTimeout(120000);
    const pageErrors = await openDemo(win);
    for (const d of CATALOG) {
      await set(win, { id: d.id });
      const p0 = await probe(win);
      expect(p0.id).toBe(d.id);
      if (d.dynamic) {
        await expect.poll(async () => (await probe(win)).frames, { message: `${d.id} renders frames`, timeout: 10000 }).toBeGreaterThan(p0.frames);
        const p = await probe(win);
        expect(p.renderer, `${d.id} ran on a real renderer`).not.toBe('css');
        expect(p.stats?.renderer).toBe(p.renderer);
      } else {
        expect(p0.renderer).toBe('css');
      }
    }
    const p = await probe(win);
    expect(p.errors).toEqual([]);
    expect(pageErrors).toEqual([]);
  });

  test('pause draws zero frames; resume continues from the paused count', async ({ win }) => {
    await openDemo(win);
    await set(win, { id: 'life', fpsCap: 30 });
    await expect.poll(async () => (await probe(win)).frames, { timeout: 10000 }).toBeGreaterThan(5);
    await engine(win, 'setRunState', 'pause');
    await win.waitForTimeout(300);
    const f1 = (await probe(win)).frames;
    await win.waitForTimeout(2000);
    expect((await probe(win)).frames).toBe(f1);
    const t0 = Date.now();
    await engine(win, 'setRunState', 'run');
    await expect.poll(async () => (await probe(win)).frames, { timeout: 5000 }).toBeGreaterThan(f1);
    const f2 = (await probe(win)).frames, secs = (Date.now() - t0) / 1000;
    // a loop that kept running through the 2 s pause would add ~2 s worth of frames on top
    expect(f2 - f1).toBeLessThanOrEqual(Math.ceil(secs * 30) + 3);
  });

  test('reduced motion: one settled frame, then nothing', async ({ win }) => {
    await openDemo(win);
    await set(win, { id: 'flow' });
    await expect.poll(async () => (await probe(win)).frames, { timeout: 10000 }).toBeGreaterThan(3);
    await engine(win, 'setReducedMotion', true);
    await win.waitForTimeout(800);
    const f1 = (await probe(win)).frames;
    await win.waitForTimeout(1500);
    expect((await probe(win)).frames).toBe(f1);
    await set(win, { id: 'tracers' });                       // a new config under reduced motion: one frame, no loop
    await expect.poll(async () => (await probe(win)).id, { timeout: 5000 }).toBe('tracers');
    await win.waitForTimeout(1500);
    const f2 = (await probe(win)).frames;
    await win.waitForTimeout(1000);
    expect((await probe(win)).frames).toBe(f2);
    expect(f2).toBeLessThanOrEqual(1);
  });

  test('preview(x) then preview(null) restores the committed config', async ({ win }) => {
    await openDemo(win);
    await set(win, { id: 'mesh' });
    expect((await probe(win)).id).toBe('mesh');
    await engine(win, 'preview', { id: 'life', seed: 3, params: {}, intensity: 0.5, speed: 1, fpsCap: 30 });
    await expect.poll(async () => (await probe(win)).id, { timeout: 5000 }).toBe('life');
    await engine(win, 'preview', null);
    await expect.poll(async () => (await probe(win)).id, { timeout: 5000 }).toBe('mesh');
    expect((await probe(win)).renderer).toBe('css');
  });

  test('a forced heavy config makes the governor downgrade within 3 s', async ({ win }) => {
    await openDemo(win);
    await set(win, { id: 'life', fpsCap: 30 });
    await expect.poll(async () => (await probe(win)).frames, { timeout: 10000 }).toBeGreaterThan(5);
    await win.evaluate(() => (window as unknown as { __bg: { engine: { debugBurn(ms: number): void } } }).__bg.engine.debugBurn(40));
    const t0 = Date.now();
    await expect.poll(async () => (await probe(win)).stats?.downgraded, { timeout: 3500, intervals: [100] }).toBe(true);
    expect(Date.now() - t0).toBeLessThan(3500);
    const s = (await probe(win)).stats!;
    expect(s.tier).toBeGreaterThanOrEqual(1);
    expect(s.reason).toMatch(/budget/);
  });

  test('thumbnails: one decodable image per catalog id, cached on the second call', async ({ win }) => {
    test.setTimeout(90000);
    await openDemo(win, 'id=solid');
    await expect.poll(() => win.evaluate(() => [...document.querySelectorAll('#thumbs img')].filter((i) => (i as HTMLImageElement).naturalWidth > 0).length),
      { timeout: 60000 }).toBe(CATALOG.length);
    const r = await win.evaluate(async () => {
      const g = (window as unknown as { __bg: { engine: { thumbnail(id: string, w: number, h: number, t: unknown): Promise<string> }; errors: string[] } }).__bg;
      const theme = { mode: 'dark', base: '#0b0c0e', fg: '#b0b4ba', accent: '#5b5bd6', accent2: '#b1a9ff' };
      const a = await g.engine.thumbnail('silk', 96, 60, theme), b = await g.engine.thumbnail('silk', 96, 60, theme);
      return { a, same: a === b, errors: g.errors.slice() };
    });
    expect(r.a.startsWith('blob:')).toBe(true);
    expect(r.same).toBe(true);
    expect(r.errors).toEqual([]);
  });

  test('perf: page rAF p95 per id (asserted < 25 ms with HARNESS_DESKTOP_E2E_PERF=1); page heap grows < 10 MB over 20 s', async ({ win }) => {
    test.setTimeout(120000);
    await openDemo(win);
    const p95 = (ms: number) => win.evaluate((dur) => new Promise<number>((res) => {
      const iv: number[] = []; let last = -1; const end = performance.now() + dur;
      const f = (t: number) => { if (last >= 0) iv.push(t - last); last = t; if (t < end) requestAnimationFrame(f); else { iv.sort((a, b) => a - b); res(iv[Math.floor(iv.length * 0.95)] ?? 0); } };
      requestAnimationFrame(f);
    }), ms);
    const results: Record<string, number> = {};
    for (const d of [...CATALOG.filter((x) => !x.dynamic).slice(0, 1), ...CATALOG.filter((x) => x.dynamic)]) {
      await set(win, { id: d.id });
      await win.waitForTimeout(1500);
      results[d.id] = await p95(2500);
    }
    console.log('rAF p95 ms', JSON.stringify(results));
    test.info().annotations.push({ type: 'rAF p95 ms', description: JSON.stringify(results) });
    // The 25 ms target is not met on software GL (SwiftShader starves the compositor; see docs/progress/L4.md),
    // so it is asserted only when HARNESS_DESKTOP_E2E_PERF=1 (a host with a real GPU). The heap check always runs.
    if (process.env.HARNESS_DESKTOP_E2E_PERF === '1') {
      for (const [id, v] of Object.entries(results)) expect.soft(v, `${id} p95 ${v} ms`).toBeLessThan(25);
    }
    await set(win, { id: 'tracers' });
    await win.waitForTimeout(1500);
    const heap = () => win.evaluate(() => (performance as unknown as { memory?: { usedJSHeapSize: number } }).memory?.usedJSHeapSize ?? 0);
    const h0 = await heap();
    await win.waitForTimeout(20000);
    const h1 = await heap();
    console.log('page heap MB', (h0 / 1e6).toFixed(1), '->', (h1 / 1e6).toFixed(1));
    expect(h0, 'performance.memory is available').toBeGreaterThan(0);
    expect(h1 - h0).toBeLessThan(10 * 1024 * 1024);
  });
});
