// W2-E: the native Findings view against the mock serve: board with details, keyboard selection, archive (one
// rolling item, selected rows), move; the agent card (state, stop with confirm); the knowledge note reader.
import { test, expect, waitReady } from '../fixtures';

const now = Date.now();
const items = [
  { id: 'f1', title: 'Crash on save', src: 'DEMO-1', key: 'DEMO-1', cat: 'bug', sev: 'high', path: 'src/a.py', line: 12, body: 'Saving **twice** crashes',
    steps: ['open a file', 'save twice'], expected: 'saved', actual: 'a crash', ms: now - 3600e3, archived: false, md: '- [high] Crash on save' },
  { id: 'f2', title: 'Unused helpers', src: 'agent', key: '', cat: 'cleanup', sev: 'low', kind: 'rolling', project: '/fake/project',
    items: [{ id: 'f2-i1', path: 'src/util.py', line: 3, note: 'never called' }], archived: false },
  { id: 'f3', title: 'An old one', src: 'agent', archived: true },
];
const agent = {
  running: true, heartbeat: Math.floor(now / 1000) - 5,
  status: { state: 'exploring', model: 'free-model', unit: 'project: src/', session: 'ses_fa000001', open: 2, counts: { bug: { calls: 3, accepted: 1, rejected: 2, why: { duplicate: 2 } } } },
  passes: [{ outcome: 'found', repo: '/fake/project', area: 'src', lens: 'bug', session: 'ses_fa000001', found: 1, started: '10:00' }],
  folders: ['/fake/project'], last_repos: ['/fake/project'], notes: 'look at the parser',
  knowledge: [{ slug: 'project-1', name: 'src.md', repo: '/fake/project', area: 'src', title: 'Source tree', lens: 'bug' }],
};

test('findings board, agent card and notes', async ({ mock, launch }) => {
  await mock.setTopic('findings', { items, order: ['f1', 'f2'] });
  await mock.setTopic('findings-agent', agent);
  await mock.setTopic('findings/notes/project-1/src.md', { text: '# Source tree\n\nThe **parser** lives here.' });
  const { win } = await launch();
  await waitReady(win, 30000);
  await win.getByTestId('rail-findings').click();
  await expect(win.getByTestId('view-findings')).toBeVisible();
  await expect(win.getByTestId('findings-head-state')).toContainText('exploring');
  await expect(win.getByTestId('finding-f1')).toBeVisible();
  const d1 = win.getByTestId('finding-detail-f1');
  await expect(d1).toContainText('save twice');
  await expect(d1.locator('strong')).toHaveText('twice');
  await win.screenshot({ path: 'test-results/w2e-findings.png' });

  // keyboard: the list selects with the arrows; a rolling item is archived on its own
  await win.getByTestId('findings-list').focus();
  await win.keyboard.press('ArrowDown');
  await expect(win.getByTestId('finding-detail-f2')).toContainText('never called');
  await win.getByTestId('finding-detail-f2').getByRole('button', { name: 'Not this one' }).click();
  await expect.poll(async () => (await mock.requests('/findings/archive')).map((r) => r.body)).toContainEqual({ id: 'f2-i1' });

  // Space ticks the selected row; Archive posts the selection; Alt+Up moves it on the board
  await win.getByTestId('findings-list').focus();
  await win.keyboard.press('Space');
  await win.getByTestId('findings-archive-selected').click();
  await expect.poll(async () => (await mock.requests('/findings/archive')).map((r) => r.body)).toContainEqual({ sel: ['f2'] });
  await win.getByTestId('findings-list').focus();
  await win.keyboard.press('Alt+ArrowUp');
  await expect.poll(async () => (await mock.requests('/findings/move')).map((r) => r.body)).toContainEqual({ key: 'f2', before: 'f1' });

  // archived tab
  await win.getByTestId('findings-tab-archived').click();
  await expect(win.getByTestId('finding-f3')).toBeVisible();

  // agent: stop asks first
  await win.getByTestId('findings-tab-agent').click();
  await expect(win.getByTestId('findings-agent-state')).toContainText('exploring');
  await expect(win.getByTestId('findings-agent-counts')).toContainText('duplicate 2');
  await win.screenshot({ path: 'test-results/w2e-findings-agent.png' });
  await win.getByTestId('findings-agent-stop').click();
  await win.getByTestId('findings-confirm-ok').click();
  await expect.poll(async () => (await mock.requests('/findings/agent/stop')).length).toBe(1);

  // notes: read one with the markdown renderer
  await win.getByTestId('findings-tab-notes').click();
  await win.getByTestId('note-project-1-src.md').click();
  await expect(win.getByTestId('findings-note-text').locator('strong')).toHaveText('parser');
  await win.screenshot({ path: 'test-results/w2e-findings-notes.png' });
});

test('findings: empty board and a stopped agent that starts with its folders', async ({ mock, launch }) => {
  await mock.setTopic('findings', { items: [], order: [] });
  await mock.setTopic('findings-agent', { running: false, heartbeat: null, status: {}, passes: [], folders: [], last_repos: ['/fake/project'], notes: '', knowledge: [] });
  const { win } = await launch();
  await waitReady(win, 30000);
  await win.getByTestId('rail-findings').click();
  await expect(win.getByTestId('view-findings').getByTestId('empty-state')).toContainText('No findings yet');
  await expect(win.getByTestId('findings-head-state')).toContainText('never started');
  await win.getByTestId('findings-head-start').click();
  await expect(win.getByTestId('findings-start-form')).toBeVisible();
  await win.getByLabel('Other folders, one per line').fill('/fake/other\n');
  await win.getByTestId('findings-start-go').click();
  await expect.poll(async () => (await mock.requests('/findings/agent/start')).map((r) => r.body))
    .toContainEqual({ pick: 1, dir: ['/fake/project'], more: ['/fake/other'] });
});
