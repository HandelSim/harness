// W2-F: native Debug view against the mock serve: list (runs + reports), keyboard selection, report text, kind filter.
import { test, expect, waitReady } from '../fixtures';

const now = Math.floor(Date.now() / 1000);
const list = [
  { kind: 'runs', name: 'DEMO-1-debug-1', mtime: now - 60, key: 'DEMO-1', verdict: 'bug', change: true, title: 'Login fails', session: 'ses_mockaaaa0001', report: true },
  { kind: 'reports', name: 'export-report', mtime: now - 3600, key: '', verdict: '', change: false, title: 'Export is slow', session: '', report: true },
  { kind: 'runs', name: 'DEMO-2-debug-1', mtime: now - 7200, key: 'DEMO-2', verdict: 'running', change: false, title: 'no report yet', session: '', report: false },
];

test('debug lists runs and reports, shows the selected report, moves with the keyboard', async ({ win, mock }) => {
  await mock.setTopic('debug', list);
  await (mock as unknown as { setRoute(k: string, d: unknown, s?: number): Promise<void> })
    .setRoute('/debug/runs/DEMO-2-debug-1', { code: 'not-found', message: 'no such report' }, 404);
  await waitReady(win, 30000);
  await win.getByTestId('rail-debug').click();
  await expect(win.getByTestId('view-debug')).toBeVisible();
  await expect(win.locator('[data-testid^="debug-row-"]')).toHaveCount(3);
  const report = win.getByTestId('debug-report');
  await expect(report.getByTestId('debug-report-md')).toContainText('session cookie is dropped');
  await win.getByTestId('debug-list').focus();
  await win.keyboard.press('ArrowDown');
  await expect(win.getByTestId('debug-row-export-report')).toHaveAttribute('aria-selected', 'true');
  await expect(report.getByTestId('debug-report-md')).toContainText('Not a bug');
  await win.keyboard.press('ArrowDown');
  await expect(report).toContainText('Still looking');
  const view = win.getByTestId('view-debug');
  await view.getByRole('button', { name: 'Reports', exact: true }).click();
  await expect(win.locator('[data-testid^="debug-row-"]')).toHaveCount(1);
  await view.getByRole('button', { name: 'All', exact: true }).click();
  await win.getByTestId('debug-row-DEMO-1-debug-1').click();
  await report.getByTestId('debug-session').click();
  await expect(win.getByTestId('tab-transcript:ses_mockaaaa0001')).toBeVisible();
  await win.screenshot({ path: 'test-results/w2f-debug.png' });
});

test('debug empty state', async ({ win }) => {
  await waitReady(win, 30000);
  await win.getByTestId('rail-debug').click();
  await expect(win.getByTestId('view-debug').getByTestId('empty-state')).toBeVisible();
});
