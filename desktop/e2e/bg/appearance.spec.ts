// review2 P1-1 / P1-2 (W3-A): accent presets reach the tokens, and an OS theme flip under theme 'system' re-runs the
// glass contrast floor (it used to keep the dark floor under a light surface until the next settings write).
import { test, expect, waitReady } from '../fixtures';
import type { Page } from '@playwright/test';

const settingsSet = (win: Page, patch: unknown) => win.evaluate(async (p) => { await (window as any).harness.settings.set(p); }, patch);
const css = (win: Page, name: string) => win.evaluate((n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim(), name);

test('accent swatch sets a preset and the --accent-* tokens resolve', async ({ win }) => {
  await waitReady(win, 30000);
  await win.getByTestId('rail-settings').click();
  await expect(win.getByTestId('view-settings')).toBeVisible();
  await settingsSet(win, { appearance: { theme: 'dark' } });
  await win.getByTestId('view-settings').getByRole('radio', { name: 'teal' }).click();
  await expect(win.locator('html')).toHaveAttribute('data-accent', 'teal');
  await expect.poll(() => css(win, '--accent-9')).toBe('#12a594');
  await expect(win.getByTestId('view-settings').getByRole('radio', { name: 'teal' })).toHaveAttribute('aria-checked', 'true');
  await win.getByTestId('view-settings').getByRole('radio', { name: 'iris' }).click();
  await expect(win.locator('html')).toHaveAttribute('data-accent', 'iris');
  await expect.poll(() => css(win, '--accent-9')).toBe('#5b5bd6');
});

test('theme system: an OS light/dark flip re-resolves the theme and the glass floor', async ({ win }) => {
  await waitReady(win, 30000);
  await win.emulateMedia({ colorScheme: 'dark' });
  await settingsSet(win, { appearance: { theme: 'system', seeThrough: 1, glass: true, accent: 'iris' } });
  await expect(win.locator('html')).toHaveAttribute('data-theme', 'dark');
  // seeThrough 1 puts the panel at the floor itself (iris dark 0.665); 0.7 is the stale seeThrough-0.5 value
  await expect.poll(async () => Number(await css(win, '--panel-alpha-auto'))).toBeLessThan(0.7);
  const dark = Number(await css(win, '--panel-alpha-auto'));
  await win.emulateMedia({ colorScheme: 'light' });
  await expect(win.locator('html')).toHaveAttribute('data-theme', 'light');
  await expect.poll(async () => Number(await css(win, '--panel-alpha-auto'))).not.toBe(dark);
  const light = Number(await css(win, '--panel-alpha-auto'));
  expect(light).toBeGreaterThan(dark);            // the light floor is higher (see surface.test.ts floors)
  await win.emulateMedia({ colorScheme: 'dark' });
  await expect(win.locator('html')).toHaveAttribute('data-theme', 'dark');
  await expect.poll(async () => Number(await css(win, '--panel-alpha-auto'))).toBe(dark);
  await settingsSet(win, { appearance: { theme: 'dark', seeThrough: 0.5 } });
});
