// W2-E: the native Home view against the mock serve: health tiles, queue summary, working now, usage, pages,
// activity, keyboard opening of a live run, and the proxy-down / no-vault states.
import { test, expect, waitReady } from '../fixtures';

const now = Date.now();
const live = { id: 'ses_live0001', parent: null, source: 'container', directory: '/fake/project', title: 'Fix login', agent: 'build', model: 'mock-model',
  created: now - 600e3, updated: now - 30e3, tokens_in: 10, tokens_out: 5, cost: 0, kids: 0, working: true, live: true, ticket: 'DEMO-3', kind: 'queue', label: 'implement', web: null };
const status = (over: Record<string, unknown> = {}) => ({ api: 1, mode: 'serve', pid: 1, state_id: 'mockstate0000000', stale: false, root: '/fake/harness', state_dir: '/fake/harness/state',
  codes: false, user: null, runs: true, opencode: { port: 0, url: '' }, proxy: { url: 'http://127.0.0.1:9', up: true, detail: 'mock' },
  vault: { name: 'demo', dir: '/fake/vault' }, modes: [{ name: 'container', description: 'in the container' }, { name: 'host', description: 'on the host' }],
  version: { branch: 'dev', sha: 'abc1234', describe: 'mock-1' }, ...over });

test('home shows the overview and opens a live run from the keyboard', async ({ mock, launch }) => {
  await mock.setTopic('home', {
    live: [live],
    usage: { day: { replies: 12, prompt: 3400, completion: 1200, reasoning: 100, cached: 50, total: 4600 }, hour: { replies: 2, total: 800 },
      models: [['big-model', [10, 3000, 1000]], ['small-model', [2, 400, 200]]], remaining: [['requests', '99']] },
    pages: [{ rel: 'reports/weekly.html', title: 'Weekly report', mtime: now / 1000, url: '/pub/reports/weekly.html' }],
    activity: [{ sha: 'abc1234', author: 'harness', when: '2 hours ago', subject: 'ingest: 3 sources' }],
  });
  const { win } = await launch();
  await waitReady(win, 30000);
  await win.getByTestId('rail-home').click();
  await expect(win.getByTestId('view-home')).toBeVisible();
  await expect(win.getByTestId('home-proxy')).toContainText('up');
  await expect(win.getByTestId('home-vault')).toContainText('demo');
  await expect(win.getByTestId('home-queue')).toContainText('queued');
  await expect(win.getByTestId('home-usage')).toContainText('big-model');
  await expect(win.getByTestId('home-usage')).toContainText(/4[,.  ]?600/);
  await expect(win.getByTestId('home-usage')).toContainText('requests 99');
  await expect(win.getByTestId('home-activity')).toContainText('ingest: 3 sources');
  await expect(win.getByTestId('home-modes')).toContainText('container');
  await expect(win.getByTestId('home-page-reports/weekly.html')).toBeVisible();
  await win.screenshot({ path: 'test-results/w2e-home.png' });
  await win.getByTestId('home-live-ses_live0001').focus();
  await win.keyboard.press('Enter');
  await expect(win.getByTestId('transcript-ses_live0001')).toBeVisible();
});

test('home: proxy down offers a backend restart, no vault and empty overview say so', async ({ mock, launch }) => {
  await mock.setTopic('status', status({ proxy: { url: 'http://127.0.0.1:9', up: false, detail: 'connection refused' }, vault: null }));
  await mock.setTopic('home', { live: [], usage: null, pages: [], activity: [] });
  const { win } = await launch();
  await waitReady(win, 30000);
  await win.getByTestId('rail-home').click();
  await expect(win.getByTestId('home-proxy')).toContainText('down');
  await expect(win.getByTestId('home-restart')).toBeVisible();
  await expect(win.getByTestId('home-vault')).toContainText('no vault selected');
  await expect(win.getByTestId('home-live')).toContainText('No session is running');
  await expect(win.getByTestId('home-usage')).toContainText('No usage recorded yet');
  await expect(win.getByTestId('home-pages')).toHaveCount(0);
  await win.screenshot({ path: 'test-results/w2e-home-empty.png' });
});
