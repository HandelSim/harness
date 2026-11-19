// W2-F: ticket Changes tab against the mock serve: files and lines, show more lines (/lines), click-a-line comment,
// send to the fixer (POST /tickets/K/fix), view switch, and a big diff that stays responsive (collapsed files, line cap).
import { test, expect, waitReady } from '../fixtures';
// @ts-expect-error plain ESM helper without types
import { ticket } from '../mock-serve/scenarios.mjs';

type Routes = { setRoute(key: string, data: unknown, status?: number): Promise<void> };
const fixable = () => { const t = ticket('DEMO-2', 'done'); t.actions.fix = { ok: true, reason: '' }; return t; };

async function openChanges(win: import('@playwright/test').Page) {
  await win.getByTestId('rail-queue').click();
  await win.getByTestId('ticket-row-DEMO-2').dblclick();
  const panel = win.getByTestId('panel-ticket:DEMO-2');
  await panel.getByTestId('ticket-tab-changes').click();
  return panel;
}

test('changes tab: diff, show more, a line comment sent with fix', async ({ win, mock }) => {
  await mock.setTopic('ticket:DEMO-2', fixable());
  await mock.onPost('/tickets/DEMO-2/fix', { body: { ok: true, msg: 'fix queued' } });
  await waitReady(win, 30000);
  const p = await openChanges(win);
  await expect(p.getByTestId('diff-file')).toHaveCount(2);
  await expect(p.getByTestId('diff-totals')).toContainText('2 files');
  await p.getByRole('button', { name: /19 more lines/ }).click();
  await expect(p.getByTestId('diff-body').first()).toContainText('line 1 of src/app.ts');
  await p.getByTestId('diff-line').filter({ hasText: 'const c = a + b;' }).click();
  await p.getByTestId('diff-comment-box').locator('textarea').fill('Name c better');
  await p.getByTestId('diff-comment-save').click();
  await expect(p.getByTestId('diff-comment')).toHaveCount(1);
  await expect(p.getByTestId('diff-count')).toHaveText('1/40');
  await p.getByTestId('diff-send').click();
  await expect.poll(async () => (await mock.requests('/tickets/DEMO-2/fix')).filter((r) => r.method === 'POST').length).toBe(1);
  const body = (await mock.requests('/tickets/DEMO-2/fix')).find((r) => r.method === 'POST')!.body as { comments: Record<string, unknown>[] };
  expect(body.comments).toEqual([{ path: 'src/app.ts', line: 22, side: 'new', code: '  const c = a + b;', text: 'Name c better' }]);
  await expect(p.getByTestId('diff-comment')).toHaveCount(0);
  await p.getByTestId('diff-view-uncommitted').click();
  await expect(p.getByTestId('empty-state')).toBeVisible();
});

test('changes tab: a big diff renders collapsed files and capped lines; keyboard comments', async ({ win, mock }) => {
  const files = Array.from({ length: 60 }, (_, f) => {
    const lines = Array.from({ length: 1000 }, (_, i) => `+line ${i} of file ${f}`);
    return `diff --git a/big/f${f}.txt b/big/f${f}.txt\nnew file mode 100644\n--- /dev/null\n+++ b/big/f${f}.txt\n@@ -0,0 +1,1000 @@\n${lines.join('\n')}`;
  }).join('\n') + '\n';
  await mock.setTopic('ticket:DEMO-2', fixable());
  await (mock as unknown as Routes).setRoute('/tickets/DEMO-2/diff', { repos: [{ rel: '.', label: 'demo' }], views: ['work', 'uncommitted', 'unpushed'],
    repo: '.', view: 'work', diff: files, commits: [], notes: '', says: '', rev: 'abc', kind: 'git', error: '' });
  await waitReady(win, 30000);
  const t0 = Date.now();
  const p = await openChanges(win);
  await expect(p.getByTestId('diff-file')).toHaveCount(60);
  expect(Date.now() - t0).toBeLessThan(15000);
  // 8 open files × FILE_LINE_CAP (400) rows, one of which is the hunk header (not a diff-line)
  expect(await p.getByTestId('diff-line').count()).toBe(8 * 399);
  await expect(p.getByTestId('diff-show-all')).toHaveCount(8);
  // keyboard: focus the first file's diff, move down twice, Enter opens a comment box, Escape closes it
  await p.getByTestId('diff-body').first().focus();
  await win.keyboard.press('ArrowDown');
  await win.keyboard.press('ArrowDown');
  await win.keyboard.press('Enter');
  await expect(p.getByTestId('diff-comment-box')).toBeVisible();
  await win.keyboard.type('keyboard note');
  await win.keyboard.press('Control+Enter');
  await expect(p.getByTestId('diff-comment')).toContainText('big/f0.txt:1');
});
