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
  // P2-6: Add tickets carries the picked keys into the dialog (no clipboard paste step)
  await win.getByTestId('jira-add').click();
  await expect(win.getByTestId('add-keys')).toHaveValue('JIRA-11');
  // parity (web add form): Target branch (`base`) and Work branch name (`branch`) reach POST /queue/add
  await mock.onPost('/queue/add', { body: { ok: true, msg: 'added' } });
  await win.getByTestId('add-base').fill('main');
  await win.getByTestId('add-branch').fill('feat/jira-11');
  await win.getByTestId('add-submit').click();
  await expect.poll(async () => (await mock.requests('/queue/add')).filter((r) => r.method === 'POST').map((r) => r.body))
    .toContainEqual(expect.objectContaining({ keys: 'JIRA-11', base: 'main', branch: 'feat/jira-11' }));
  // a successful add closes the dialog
  await expect(win.getByTestId('add-keys')).toHaveCount(0);
  await win.getByTestId('jira-filter').fill('dark');
  await expect(rows.locator('[data-testid^="jira-row-"]')).toHaveCount(1);
  // review 2 P2-1: Space and Enter in the filter are text, not "pick/open the row under the cursor"
  await win.getByTestId('jira-filter').press(' ');
  await win.getByTestId('jira-filter').press('Enter');
  await expect(win.getByTestId('jira-filter')).toHaveValue('dark ');
  await expect(win.getByTestId('jira-copy')).toContainText('1');
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
