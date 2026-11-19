// W2-E: the native Chat panel against the mock serve: live runs first, the transcript embedded with its composer
// (POST /sessions/<sid>/send), keyboard selection, an ended host session read-only with opencode web offered.
import { test, expect, waitReady } from '../fixtures';

const now = Date.now();
const row = (id: string, over: Record<string, unknown> = {}) => ({ id, parent: null, source: 'container', directory: '/fake/project', title: id, agent: 'build',
  model: 'mock-model', created: now - 600e3, updated: now - 60e3, tokens_in: 0, tokens_out: 0, cost: 0, kids: 0, working: false, live: false, ticket: null,
  kind: 'session', label: '', web: null, ...over });
const liveRow = row('ses_live0001', { title: 'Fix login', live: true, updated: now - 5e3 });
const oldRow = row('ses_old00002', { title: 'Old chat', source: 'host', updated: now - 7200e3, web: 'http://127.0.0.1:4096/server/x/session/ses_old00002' });
const msg = (index: number, role: string, text: string) => ({ id: `m${index}`, index, created: now - 10e3 + index, role, info: { role }, parts: [{ id: `p${index}`, type: 'text', text }] });

test('chat lists live runs first and sends through the transcript composer', async ({ mock, launch }) => {
  await mock.setTopic('sessions', [oldRow, liveRow]);
  await mock.setTopic('session:ses_live0001', { row: liveRow, count: 2, children: [], inbox: [], send: { ok: true, reason: '' }, stop: { ok: false, reason: 'not working' } });
  await mock.setTopic('session:ses_old00002', { row: oldRow, count: 1, children: [], inbox: [], send: { ok: false, reason: 'this run is not live (finished, or not a headless run)' }, stop: { ok: false, reason: 'not working' } });
  await mock.setTopic('sessions/ses_live0001/messages', { from: 0, count: 2, live: true, working: false, messages: [msg(0, 'user', 'hello agent'), msg(1, 'assistant', 'hi, on it')] });
  await mock.setTopic('sessions/ses_old00002/messages', { from: 0, count: 1, live: false, working: false, messages: [msg(0, 'user', 'an old question')] });
  const { win } = await launch();
  await waitReady(win, 30000);
  await win.getByTestId('rail-home').click();
  await win.getByTestId('home-open-chat').click();
  await expect(win.getByTestId('view-chat')).toBeVisible();
  const first = win.getByTestId('chat-list').locator('[role=option]').first();
  await expect(first).toHaveAttribute('data-testid', 'chat-row-ses_live0001');
  await expect(first).toHaveAttribute('aria-selected', 'true');
  const t = win.getByTestId('transcript-ses_live0001');
  await expect(t).toContainText('hi, on it');
  await t.getByLabel('Message').fill('please add a test');
  await t.getByLabel('Message').press('Control+Enter');
  await expect.poll(async () => (await mock.requests('/sessions/ses_live0001/send')).map((r) => r.body)).toContainEqual({ text: 'please add a test' });
  await win.screenshot({ path: 'test-results/w2e-chat.png' });

  // keyboard: the next session is read-only and offers opencode web
  await win.getByTestId('chat-list').focus();
  await win.keyboard.press('ArrowDown');
  await expect(win.getByTestId('chat-row-ses_old00002')).toHaveAttribute('aria-selected', 'true');
  await expect(win.getByTestId('transcript-ses_old00002')).toContainText('an old question');
  await expect(win.getByTestId('chat-readonly')).toBeVisible();
  await expect(win.getByTestId('chat-open-oc')).toBeVisible();
  // review 2 P2-5: stepping through the list quickly does not mount (and page in) each row's transcript
  const liveGets = (await mock.requests('/sessions/ses_live0001/messages')).length;
  await win.keyboard.press('ArrowUp');
  await win.keyboard.press('ArrowDown');
  await win.waitForTimeout(600);
  await expect(win.getByTestId('chat-row-ses_old00002')).toHaveAttribute('aria-selected', 'true');
  expect((await mock.requests('/sessions/ses_live0001/messages')).length).toBe(liveGets);

  // filter and the Live scope
  await win.getByTestId('chat-filter').fill('old');
  await expect(win.getByTestId('chat-row-ses_live0001')).toHaveCount(0);
  await win.getByTestId('chat-filter').fill('');
  await win.getByTestId('view-chat').getByRole('button', { name: 'Live', exact: true }).click();
  await expect(win.getByTestId('chat-row-ses_old00002')).toHaveCount(0);
  await expect(win.getByTestId('chat-row-ses_live0001')).toBeVisible();
});

test('chat: no sessions shows the empty state', async ({ mock, launch }) => {
  await mock.setTopic('sessions', []);
  const { win } = await launch();
  await waitReady(win, 30000);
  await win.keyboard.press('Control+K');
  // the palette resets its query in an effect and focuses the input after it; fill only once that has happened
  await expect(win.getByTestId('palette-input')).toBeFocused();
  await win.getByTestId('palette-input').fill('Open chat');
  await expect(win.getByTestId('palette').locator('[role=option][aria-selected=true]')).toContainText('Open chat');
  await win.keyboard.press('Enter');
  await expect(win.getByTestId('view-chat')).toBeVisible();
  await expect(win.getByTestId('view-chat').getByTestId('empty-state').first()).toContainText('No sessions yet');
});
