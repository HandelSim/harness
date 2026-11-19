import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseConfigTsv, maskedChars, missingRequired, parseCheckUpdates, framer } from './cli';
import { buildArgv, cleanSpec } from './pty/spec';
import { unsupportedReason } from './updater';
import { locksAt, reminders, DAY } from './keys/lifecycle';
import { apiBase, classifyProbe, classifyUnlock } from './keys/probe';

const tsv = (rows: [string, string, string][]) => rows.map(([k, v, s]) => `${k}\t${v}\t${s}\tdesc`).join('\n');

test('config list --tsv parsing and masked values', () => {
  const rows = parseConfigTsv(tsv([['PROXY_API_URL', 'http://u/v1', '0'], ['PROXY_API_KEY', '(set, 24 chars)', '1'], ['DEFAULT_MODEL_NAME', '', '0']]));
  assert.equal(rows.length, 3);
  assert.equal(rows[1].secret, true);
  assert.equal(maskedChars(rows[1].value), 24);
  assert.equal(maskedChars(''), 0);
  assert.deepEqual(missingRequired(rows), ['DEFAULT_MODEL_NAME']);
  const oai = parseConfigTsv(tsv([['OAI_API_URL', 'http://o', '0'], ['OAI_API_KEY', '', '1'], ['OAI_MODELS', 'm', '0'], ['PROXY_API_URL', '', '0'], ['PROXY_API_KEY', '', '1']]));
  assert.deepEqual(missingRequired(oai), ['OAI_API_KEY']);
});
test('check-updates wording', () => {
  const a = parseCheckUpdates('[harness] up to date with origin/dev (abc1234)\n', 0, 1);
  assert.deepEqual([a.available, a.branch, a.local], [false, 'dev', 'abc1234']);
  const b = parseCheckUpdates("[harness] update available on dev; run 'harness upgrade' to update\n", 0, 1);
  assert.deepEqual([b.available, b.branch], [true, 'dev']);
  assert.equal(parseCheckUpdates('fatal: no network', 1, 1).available, null);
});
test('framer never splits a UTF-8 character', async () => {
  const got: string[] = [];
  const f = framer((s) => got.push(s), 1);
  const b = Buffer.from('a€b');
  f(b.subarray(0, 2)); f(b.subarray(2));
  f.flush();
  assert.equal(got.join(''), 'a€b');
  assert.ok(!got.join('').includes('�'));
});
test('pty argv on Windows: Git Bash runs harness and the plain terminal', () => {
  const o = { root: 'C:/Users/me/harness', shell: undefined, hostConsent: false, isDir: () => true, platform: 'win32' as const,
    bash: 'C:\\Program Files\\Git\\bin\\bash.exe' };
  const p = buildArgv(cleanSpec({ kind: 'plain', cwd: 'C:\\Users\\me' })!, o);
  assert.ok(p.ok && p.argv.file === o.bash && p.argv.args.join(' ') === '--login -i');
  const a = buildArgv(cleanSpec({ kind: 'agent', cwd: 'C:\\Users\\me' })!, o);
  assert.ok(a.ok && a.argv.file === o.bash && a.argv.args[0] === 'C:/Users/me/harness/harness');
});
test('pty argv: kinds, flags, resume, refusals', () => {
  const o = { root: '/r', shell: '/bin/zsh', hostConsent: true, isDir: () => true };
  const spec = (x: object) => cleanSpec({ kind: 'agent', cwd: '/p', ...x })!;
  assert.deepEqual(buildArgv(spec({ kind: 'plain' }), o), { ok: true, argv: { file: '/bin/zsh', args: [], cwd: '/p', env: { TERM: 'xterm-256color', COLORTERM: 'truecolor', HARNESS_OPEN: '0' } } });
  const a = buildArgv(spec({ yolo: true, net: true, ticket: 'abc-12', resume: 'last' }), o);
  assert.ok(a.ok); assert.deepEqual(a.ok && a.argv.args, ['/r/harness', '--yolo', '--net', '--ticket', 'ABC-12', '--continue']);
  const h = buildArgv(spec({ kind: 'host', resume: { session: 'ses_abc123' } }), o);
  assert.ok(h.ok && h.argv.args.join(' ') === '/r/harness host --session ses_abc123' && h.argv.env.HARNESS_HOST_CONFIRM === '1');
  assert.equal(buildArgv(spec({ kind: 'host', net: true }), o).ok, false);
  assert.equal(buildArgv(spec({ ticket: 'nope' }), o).ok, false);
  assert.equal(buildArgv(spec({ resume: { session: 'ses_a;rm' } }), o).ok, false);
  assert.equal(buildArgv(spec({ cwd: 'relative' }), o).ok, false);
  assert.equal(buildArgv(spec({}), { ...o, root: null }).ok, false);
});
test('updater unsupported reasons', () => {
  const b = { packaged: true, feedFromEnv: false, noUpdate: false, platform: 'linux', appImage: true };
  assert.equal(unsupportedReason(b), '');
  assert.match(unsupportedReason({ ...b, appImage: false }), /AppImage/);
  assert.match(unsupportedReason({ ...b, packaged: false }), /development/);
  assert.equal(unsupportedReason({ ...b, packaged: false, feedFromEnv: true }), '');
  assert.match(unsupportedReason({ ...b, noUpdate: true }), /off/);
});
test('key lifecycle: 14 days from the later of created / last unlock; reminders', () => {
  assert.equal(locksAt(0, null), 14 * DAY);
  assert.equal(locksAt(0, 5 * DAY), 19 * DAY);
  assert.equal(locksAt(null, null), null);
  const r = reminders({ label: 'k', createdAt: 0, lastUnlockAt: null, expiresAt: 30 * DAY }, DAY);
  assert.deepEqual(r.map((x) => x.at), [12 * DAY, 23 * DAY, 29 * DAY]);
});
test('key probe classification (the _probe_upstream_auth table)', () => {
  assert.equal(apiBase('https://u.example/v1/chat/completions/'), 'https://u.example');
  assert.equal(apiBase('https://u.example/v1'), 'https://u.example');
  assert.equal(classifyProbe(200, '{}', 1).state, 'ok');
  const l = classifyProbe(401, '{"error":{"type":"unauthorized","message":"API key locked","unlock_url":"https://u/x"}}', 1);
  assert.deepEqual([l.state, l.unlockUrl], ['locked', 'https://u/x']);
  assert.equal(classifyProbe(401, '{"error":{"type":"unauthorized","message":"bad key"}}', 1).state, 'rejected');
  assert.equal(classifyProbe(400, '{"error":{"type":"invalid_request_error"}}', 1).state, 'unknown');
  assert.equal(classifyProbe(503, 'down', 1).state, 'unknown');
  assert.equal(classifyUnlock(0, '[harness] upstream API key is OK; no unlock needed', 1).state, 'ok');
  const u = classifyUnlock(1, ' ERROR: Upstream API key is locked\n Visit this URL in your browser to unlock the key:\n   https://u/k\n', 1);
  assert.deepEqual([u.state, u.unlockUrl], ['locked', 'https://u/k']);
  assert.equal(classifyUnlock(1, 'ERROR: Upstream rejected the API key (HTTP 401)', 1).state, 'rejected');
});
