// W2-F: native Jira view against the mock serve: list, mine filter, queued pill, keyboard pick + copy, backlog note,
// error state.
import { test, expect, waitReady } from '../fixtures';

type Routes = { setRoute(key: string, data: unknown, status?: number): Promise<void> };

test('jira lists rows, filters to mine, picks by keyboard, shows notes and errors', async ({ win, mock }) => {
  await waitReady(win, 30000);
  await win.getByTestId('rail-jira').click();
  await expect(win.getByTestId('view-jira')).toBeVisible();
  const rows = win.getByTestId('jira-rows');
  await expect(rows.locator('[data-testid^="jira-row-"]')).toHaveCount(4);
  await expect(win.getByTestId('jira-queued-DEMO-1')).toHaveText('queued: queued');
  await win.getByTestId('jira-mine').check();
  await expect(rows.locator('[data-testid^="jira-row-"]')).toHaveCount(2);
  expect((await mock.requests('/jira')).some((r) => r.path.includes('mine=1'))).toBe(true);
  // keyboard: Down to the second row (JIRA-11), Space picks it; Copy counts it
  await rows.focus();
  await win.keyboard.press('ArrowDown');
  await win.keyboard.press(' ');
  await expect(win.getByTestId('jira-row-JIRA-11')).toHaveAttribute('aria-selected', 'true');
  await expect(win.getByTestId('jira-copy')).toContainText('1');
  await win.getByTestId('jira-filter').fill('dark');
  await expect(rows.locator('[data-testid^="jira-row-"]')).toHaveCount(1);
  await win.getByTestId('jira-filter').fill('');
  await win.getByTestId('jira-mine').uncheck();
  await win.getByTestId('view-jira').getByRole('button', { name: 'Backlog', exact: true }).click();
  await expect(win.getByTestId('jira-note')).toContainText('earlier list');
  await expect(win.getByTestId('view-jira').getByTestId('empty-state')).toBeVisible();
  await (mock as unknown as Routes).setRoute('/jira', { code: 'boom', message: 'Jira is down' }, 500);
  await (mock as unknown as Routes).setRoute('/jira?list=sprint', { code: 'boom', message: 'Jira is down' }, 500);
  await win.getByTestId('view-jira').getByRole('button', { name: 'Sprint', exact: true }).click();
  await expect(win.getByTestId('view-jira').locator('.inline-err, [data-testid="error-panel"]').first()).toContainText('Jira is down');
  await win.screenshot({ path: 'test-results/w2f-jira.png' });
});
