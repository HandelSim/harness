// W2-G: the background shows through the app's surfaces in the default layout, and text keeps >= 4.5:1.
// 1 (always): chrome + dock groups are translucent with glass on, opaque with see-through 0 or glass off.
// 2 (HARNESS_DESKTOP_E2E_SHOTS=1): screenshots of the default layout with Queue open, dark + light, 3 backgrounds,
//   into docs/progress/bg-shots/ (git-ignored), plus a pixel check: in text-free strips of the sidebar and the queue
//   panel, text-3 against the darkest and brightest composited pixel must stay >= 4.5:1.
import { test, expect, waitReady } from '../fixtures';
import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import type { Page } from '@playwright/test';

// eslint-disable-next-line @typescript-eslint/no-require-imports
const { PNG } = require('playwright-core/lib/utilsBundle') as { PNG: { sync: { read(b: Buffer): { width: number; height: number; data: Buffer } } } };

const alphaOf = (win: Page, sel: string) => win.evaluate((s) => {
  const el = document.querySelector(s);
  if (!el) return -1;
  const c = getComputedStyle(el).backgroundColor;          // rgba(r, g, b, a) | rgb(r, g, b) | color(srgb r g b / a)
  const m = /\/\s*([\d.]+)\s*\)$/.exec(c) || /rgba\([^,]+,[^,]+,[^,]+,\s*([\d.]+)\)/.exec(c);
  return m ? Number(m[1]) : 1;
}, sel);
const settingsSet = (win: Page, patch: unknown) => win.evaluate(async (p) => { await (window as any).harness.settings.set(p); }, patch);

test('surfaces are translucent by default and opaque when see-through is 0 or glass is off', async ({ win }) => {
  await waitReady(win, 30000);
  await win.getByTestId('rail-queue').click();
  await expect(win.getByTestId('view-queue')).toBeVisible();
  await win.waitForTimeout(300);
  for (const sel of ['.titlebar', '.rail', '.sidebar', '.statusbar', '.dv-groupview']) {
    const a = await alphaOf(win, sel);
    expect(a, sel).toBeGreaterThan(0.2);
    expect(a, sel).toBeLessThan(0.95);
  }
  expect(await alphaOf(win, '.dv-groupview .view'), 'no stacked panel layer').toBe(0);
  await settingsSet(win, { appearance: { seeThrough: 0 } });
  await expect.poll(() => alphaOf(win, '.dv-groupview')).toBe(1);
  await expect.poll(() => alphaOf(win, '.titlebar')).toBe(1);
  await settingsSet(win, { appearance: { seeThrough: 1, glass: false } });
  await expect.poll(() => alphaOf(win, '.dv-groupview')).toBe(1);
  await expect.poll(() => alphaOf(win, '.sidebar')).toBe(1);
});

const lin = (x: number) => (x <= 0.04045 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4);
const lum = (r: number, g: number, b: number) => 0.2126 * lin(r / 255) + 0.7152 * lin(g / 255) + 0.0722 * lin(b / 255);
const ratio = (a: number, b: number) => (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);

test('screenshots: default layout, Queue open, dark + light x 3 backgrounds', async ({ win }, info) => {
  test.skip(process.env.HARNESS_DESKTOP_E2E_SHOTS !== '1', 'set HARNESS_DESKTOP_E2E_SHOTS=1');
  test.setTimeout(180000);
  const out = join(__dirname, '..', '..', 'docs', 'progress', 'bg-shots');
  mkdirSync(out, { recursive: true });
  await waitReady(win, 30000);
  await win.getByTestId('rail-queue').click();
  await expect(win.getByTestId('view-queue')).toBeVisible();
  const ids = (process.env.HARNESS_DESKTOP_E2E_SHOT_IDS || 'mesh,attractor,flow').split(',');
  const report: unknown[] = [];
  for (const theme of ['dark', 'light'] as const) {
    for (const id of ids) {
      await settingsSet(win, { appearance: { theme }, background: { config: { id, seed: 7, params: {}, intensity: 0.6, speed: 1, fpsCap: 30 } } });
      await win.waitForTimeout(id === 'mesh' ? 1500 : 6000);
      const buf = await win.screenshot();
      writeFileSync(join(out, `shell-${theme}-${id}.png`), buf);
      const meta = await win.evaluate(async () => {
        const box = (s: string) => { const r = document.querySelector(s)?.getBoundingClientRect(); return r ? { x: r.x, y: r.y, w: r.width, h: r.height } : null; };
        const rows = [...document.querySelectorAll('[data-testid^="ticket-row-"]')].map((e) => e.getBoundingClientRect().bottom);
        const cs = getComputedStyle(document.documentElement);
        return { sidebar: box('.sidebar'), view: box('[data-testid="view-queue"]'), rowsBottom: Math.max(0, ...rows),
          text3: cs.getPropertyValue('--text-3').trim(), dpr: devicePixelRatio,
          panelAlpha: document.documentElement.style.getPropertyValue('--panel-alpha-auto'),
          chromeAlpha: document.documentElement.style.getPropertyValue('--chrome-alpha-auto'),
          bg: document.getElementById('bg-root')?.dataset ?? {},
          seeThrough: (await (window as any).harness.settings.get())?.appearance?.seeThrough };
      });
      const png = PNG.sync.read(buf);
      const t3 = meta.text3.match(/[0-9a-f]{2}/gi)!.map((h) => parseInt(h, 16));
      const lt = lum(t3[0], t3[1], t3[2]);
      const strip = (x0: number, y0: number, x1: number, y1: number) => {
        let lo = 1, hi = 0, n = 0;
        const s = meta.dpr;
        for (let y = Math.floor(y0 * s); y < Math.floor(y1 * s); y += 2) for (let x = Math.floor(x0 * s); x < Math.floor(x1 * s); x += 2) {
          const i = (y * png.width + x) * 4; const l = lum(png.data[i], png.data[i + 1], png.data[i + 2]);
          if (l < lo) lo = l; if (l > hi) hi = l; n++;
        }
        return { lo, hi, n, worst: Math.min(ratio(lt, lo), ratio(lt, hi)) };
      };
      const sb = meta.sidebar!, v = meta.view!;
      const side = strip(sb.x + 8, sb.y + sb.h - 90, sb.x + sb.w - 8, sb.y + sb.h - 10);
      const panel = strip(v.x + 8, Math.max(meta.rowsBottom + 12, v.y + v.h - 120), v.x + v.w - 8, v.y + v.h - 8);
      const r = { theme, id, seeThrough: meta.seeThrough, panelAlpha: meta.panelAlpha, chromeAlpha: meta.chromeAlpha, bg: meta.bg, side, panel };
      report.push(r);
      info.annotations.push({ type: `shot ${theme}/${id}`, description: JSON.stringify(r) });
      expect(side.worst, `${theme}/${id} sidebar text-3 contrast`).toBeGreaterThanOrEqual(4.5);
      expect(panel.worst, `${theme}/${id} panel text-3 contrast`).toBeGreaterThanOrEqual(4.5);
    }
  }
  writeFileSync(join(out, 'shell-report.json'), JSON.stringify(report, null, 1));
});
