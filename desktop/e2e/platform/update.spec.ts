// App self-update (6.4) against the mock feed (generic provider): check finds a newer version or none.
// The install step cannot be tested on any platform here (PLAN 8); download is not attempted (the feed's file is fake).
import { test, expect, waitReady } from '../fixtures';

test('generic feed: newer version is available, same version is not', async ({ launch, mock }) => {
  await mock.setFeed({ version: '9.9.9' });
  const { win } = await launch({ env: { HARNESS_DESKTOP_UPDATE_URL: `${mock.url}/feed/`, HARNESS_DESKTOP_NO_UPDATE: '0' } });
  await waitReady(win);
  const s0 = await win.evaluate(() => window.harness.update.get());
  expect(s0.phase).toBe('idle');
  const s1 = await win.evaluate(() => window.harness.update.check());
  expect(s1.phase, s1.reason).toBe('available');
  expect(s1.next).toBe('9.9.9');
  const feed = (await mock.requests('/feed/')).map((r) => r.path);
  expect(feed.some((p) => /latest-linux(-arm64)?\.yml/.test(p))).toBe(true);
  await mock.setFeed({ version: s0.current });
  const s2 = await win.evaluate(() => window.harness.update.check());
  expect(s2.phase, s2.reason).toBe('idle');
});

test('updates off: unsupported with a reason', async ({ launch }) => {
  const { win } = await launch();
  await waitReady(win);
  const s = await win.evaluate(() => window.harness.update.get());
  expect(s.phase).toBe('unsupported');
  expect(s.reason).toMatch(/off/);
});
