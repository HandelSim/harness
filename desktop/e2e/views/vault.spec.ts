// W2-F: native Vault view and vault-file panel against the mock serve (data/w2f-views.json): windowed page list with
// filter and keyboard, in-app links (relative, wiki link, image), files tree, ingest status + stop.
import { test, expect, waitReady } from '../fixtures';

const pages = [{ rel: 'wiki/index.md', title: 'Index' }, { rel: 'wiki/notes/plan.md', title: 'The plan' },
  ...Array.from({ length: 400 }, (_, i) => ({ rel: `wiki/gen/page-${String(i).padStart(3, '0')}.md`, title: `Generated ${i}` }))];

test('vault pages: windowed list, filter + Enter opens a page; its links open in-app', async ({ win, mock }) => {
  await mock.setTopic('vault', { name: 'demo', dir: '/fake/vault', facts: {}, pages, ingest: { run: null, summary: null, out_tail: '' }, log: [] });
  await waitReady(win, 30000);
  await win.getByTestId('rail-vault').click();
  await expect(win.getByTestId('view-vault')).toBeVisible();
  await expect(win.getByTestId('vault-name')).toHaveText('demo');
  const list = win.getByTestId('vault-pages');
  await expect(list).toHaveAttribute('data-virtual', '1');
  expect(await list.getByTestId('vault-page').count()).toBeLessThan(120);
  await win.getByTestId('vault-filter').fill('index');
  await expect(list.getByTestId('vault-page')).toHaveCount(1);
  await win.getByTestId('vault-filter').press('Enter');
  const idx = win.getByTestId('panel-vault-file:wiki/index.md');
  await expect(idx.getByTestId('vault-file-md')).toContainText('Welcome to the demo vault');
  await expect(idx.getByTestId('vault-file-md').locator('a', { hasText: 'Plan by wiki link' })).toHaveAttribute('href', '#vault:wiki%2Fnotes%2Fplan.md');
  await expect(idx.getByTestId('vault-file-md')).toContainText('[[plan]] stays code');
  await idx.getByRole('link', { name: 'The plan' }).click();
  await expect(win.getByTestId('panel-vault-file:wiki/notes/plan.md').getByTestId('vault-file-md')).toContainText('Ship it');
  await win.getByTestId('tab-vault-file:wiki/index.md').click();
  await idx.getByRole('link', { name: 'image: A picture' }).click();
  const img = win.getByTestId('panel-vault-file:img/pic.png').getByTestId('vault-file-img');
  await expect(img).toBeVisible();
  await expect(img).toHaveAttribute('src', /^blob:/);
  await win.screenshot({ path: 'test-results/w2f-vault-file.png' });
  // review 2 P2-6: a very large page is shown as plain text first, rendered on request
  await (mock as unknown as { setRoute(k: string, d: unknown): Promise<void> }).setRoute('/vault/file?path=wiki/notes/plan.md',
    { path: 'wiki/notes/plan.md', kind: 'markdown', text: '# Big **page**\n\n' + 'word '.repeat(70000) });
  const plan = win.getByTestId('panel-vault-file:wiki/notes/plan.md');
  await win.getByTestId('tab-vault-file:wiki/notes/plan.md').click();
  await plan.getByRole('button', { name: 'Reload' }).click();
  await expect(plan.getByTestId('md-too-big')).toBeVisible();
  await expect(plan.getByTestId('vault-file-md').locator('strong')).toHaveCount(0);
  await plan.getByTestId('md-render-anyway').click();
  await expect(plan.getByTestId('vault-file-md').locator('strong')).toHaveText('page');
});

test('vault files tree expands lazily and opens a text file; ingest shows status and stops', async ({ win, mock }) => {
  await mock.setTopic('ingest', { run: { alive: true, pid: 1, started: '2026-11-19T08:00:00Z' },
    summary: { has: true, nfailed: 1, last: '2026-11-19 ingest: 3 pages', sources: [{ name: 'demo-repo', path: '/src/demo', exists: true, ok: 3, failed: 1, pages: 3, at: '2026-11-19T08:00:00Z' }],
      failed: [{ id: 'code:demo-repo/a.ts', page: '', attempts: 2, at: '2026-11-19T08:00:00Z', problems: ['timed out'] }] },
    out_tail: 'ingesting demo-repo...' });
  await mock.onPost('/vault/ingest/stop', { body: { ok: true, msg: 'stopped' } });
  await waitReady(win, 30000);
  await win.getByTestId('rail-vault').click();
  await win.getByTestId('vault-tab-files').click();
  const tree = win.getByTestId('vault-tree');
  await expect(tree.getByTestId('vault-tree-row')).toHaveCount(3);
  await tree.getByTestId('vault-tree-row').filter({ hasText: 'wiki' }).click();
  await expect(tree.getByTestId('vault-tree-row')).toHaveCount(5);
  // review 2 P2-4: Reload refetches the tree too (it used to keep the old one until the tab was left)
  const treeGets = (await mock.requests('/vault/tree')).length;
  await win.getByTestId('vault-reload').click();
  await expect(tree.getByTestId('vault-tree-row')).toHaveCount(3);
  await expect.poll(async () => (await mock.requests('/vault/tree')).length).toBeGreaterThan(treeGets);
  await tree.getByTestId('vault-tree-row').filter({ hasText: 'wiki' }).click();
  await expect(tree.getByTestId('vault-tree-row')).toHaveCount(5);
  // keyboard: the focused tree moves with arrows and opens with Enter
  await tree.focus();
  for (let i = 0; i < 4; i++) await win.keyboard.press('ArrowDown');
  await win.keyboard.press('Enter');
  const readme = win.getByTestId('panel-vault-file:README.md');
  await expect(readme.getByTestId('vault-file-text')).toContainText('plain text readme');
  await expect(readme.getByTestId('vault-file-cut')).toBeVisible();
  await win.getByTestId('tab-view.vault').click();
  await win.getByTestId('vault-tab-ingest').click();
  await expect(win.getByTestId('vault-ingest-status')).toHaveText('Ingest running');
  await expect(win.getByTestId('vault-ingest')).toContainText('demo-repo');
  await expect(win.getByTestId('vault-ingest-tail')).toContainText('ingesting demo-repo');
  await win.getByTestId('vault-ingest-stop').click();
  await expect.poll(async () => (await mock.requests('/vault/ingest/stop')).filter((r) => r.method === 'POST').length).toBe(1);
});
