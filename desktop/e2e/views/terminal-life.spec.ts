// H3: a terminal panel's pty survives a page reload (re-attach), a pty no panel claims is reaped at renderer start,
// and a layout reset (dispose without the close confirm) kills the panel's pty instead of orphaning it.
import { test, expect, waitReady } from '../fixtures';

type Info = { id: string; exited: unknown };
const alive = async (win: import('@playwright/test').Page, id: string) =>
  win.evaluate(async (pid) => ((await window.harness.pty.list()) as Info[]).some((p) => p.id === pid && !p.exited), id);

test('terminal pty: kept across reload, orphans reaped at start, killed on layout reset', async ({ win }) => {
  test.setTimeout(90000);
  await waitReady(win);
  await win.keyboard.press('Control+T');
  await expect(win.getByTestId('dialog-new-session')).toBeVisible();
  await win.getByTestId('ns-folder').fill('/tmp');
  await win.getByTestId('ns-kind-plain').click();
  await win.getByTestId('ns-start').click();
  const term = win.locator('[data-testid^="terminal-"]:not([data-testid="terminal-starting"])').first();
  await expect(term).toBeVisible({ timeout: 20000 });
  const id = (await term.getAttribute('data-testid'))!.slice('terminal-'.length);
  expect(await alive(win, id)).toBe(true);

  // an orphan: a pty with no panel (as after a lost layout); give the layout save (300 + 500 ms debounce) time
  const orphan = await win.evaluate(async () => { const s = await window.harness.pty.spawn({ kind: 'plain', cwd: '/tmp' }, 80, 24); return s.ok ? s.id : ''; });
  expect(orphan).not.toBe('');
  await win.waitForTimeout(1500);
  await win.reload();
  await waitReady(win);
  await expect(win.getByTestId(`terminal-${id}`)).toBeVisible({ timeout: 20000 });
  await expect.poll(() => alive(win, orphan), { timeout: 10000 }).toBe(false);
  expect(await alive(win, id)).toBe(true);

  // focus is in the xterm after the reload, where Ctrl+K belongs to the shell (W18): open the palette with its button
  await win.getByRole('button', { name: 'Open command palette' }).click();
  await win.getByTestId('palette-input').fill('Reset layout');
  await win.keyboard.press('Enter');
  await expect(win.getByTestId(`terminal-${id}`)).toHaveCount(0);
  await expect.poll(() => alive(win, id), { timeout: 10000 }).toBe(false);
});
