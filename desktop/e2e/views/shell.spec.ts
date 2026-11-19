// L3 renderer smoke (PLAN 8): shell mounts, rail routes, the queue lists the mock's tickets, the palette opens,
// a double-click opens the ticket panel, and the Add tickets dialog refuses inline without losing fields.
import { test, expect, waitReady } from '../fixtures';

test('shell mounts and sets data-ready', async ({ win }) => {
  expect(await waitReady(win, 30000)).toBe('data-ready');
  await expect(win.getByTestId('rail-queue')).toBeVisible();
  await expect(win.getByTestId('status-backend')).toBeVisible();
});

test('queue view lists tickets and opens a ticket panel', async ({ win }) => {
  await waitReady(win, 30000);
  await win.getByTestId('rail-queue').click();
  await expect(win.getByTestId('view-queue')).toBeVisible();
  await expect(win.getByTestId('ticket-row-DEMO-1')).toBeVisible();
  await expect(win.getByTestId('ticket-row-DEMO-1')).toContainText('queued');
  await win.getByTestId('ticket-row-DEMO-1').click();
  await win.waitForTimeout(400);
  await win.screenshot({ path: 'test-results/l3-queue-list.png' });
  await win.getByTestId('ticket-row-DEMO-1').dblclick();
  await expect(win.getByTestId('tab-ticket:DEMO-1')).toBeVisible();
  await win.screenshot({ path: 'test-results/l3-queue.png' });
});

test('palette opens with Mod+K and closes with Escape', async ({ win }) => {
  await waitReady(win, 30000);
  await win.keyboard.press('Control+K');
  await expect(win.getByTestId('palette')).toBeVisible();
  await win.getByTestId('palette-input').fill('queue');
  await win.keyboard.press('Escape');
  await expect(win.getByTestId('palette')).toBeHidden();
});

test('add tickets: a refusal is shown inline and the fields are kept', async ({ win, mock }) => {
  await waitReady(win, 30000);
  await mock.onPost('/api/v1/queue/add', { status: 400, body: { ok: false, error: { code: 'invalid', message: 'not a ticket key (like ABC-123): nope' } } });
  await win.getByTestId('rail-queue').click();
  await win.getByTestId('queue-add').click();
  await expect(win.getByTestId('dialog-add-tickets')).toBeVisible();
  await win.getByTestId('add-keys').fill('nope');
  await win.getByTestId('add-submit').click();
  await expect(win.getByTestId('add-error')).toContainText('not a ticket key');
  await expect(win.getByTestId('add-keys')).toHaveValue('nope');
});

test('settings view is native and switches the theme', async ({ win }) => {
  await waitReady(win, 30000);
  await win.getByTestId('rail-settings').click();
  await expect(win.getByTestId('view-settings')).toBeVisible();
  await win.getByTestId('view-settings').getByRole('button', { name: 'Light', exact: true }).click();
  await expect(win.locator('html')).toHaveAttribute('data-theme', 'light');
  await win.screenshot({ path: 'test-results/l3-settings-light.png' });
  await win.getByTestId('view-settings').getByRole('button', { name: 'Dark', exact: true }).click();
  await expect(win.locator('html')).toHaveAttribute('data-theme', 'dark');
});

test('sessions view is native and a row opens its transcript panel', async ({ win }) => {
  await waitReady(win, 30000);
  await win.getByTestId('rail-sessions').click();
  await expect(win.getByTestId('view-sessions')).toBeVisible();
  const row = win.locator('[data-testid^="session-row-"]').first();
  await expect(row).toBeVisible();
  const id = (await row.getAttribute('data-testid'))!.slice('session-row-'.length);
  await expect(row.locator('b, script')).toHaveCount(0);
  await row.click();
  await expect(win.getByTestId(`transcript-${id}`)).toBeVisible();
  await expect(win.getByTestId(`tab-transcript:${id}`)).toBeVisible();
  await win.waitForTimeout(300);
  await win.screenshot({ path: 'test-results/l3-transcript.png' });
});
