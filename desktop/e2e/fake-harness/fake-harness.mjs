// The fake harness CLI. argv: <install root> <harness args...>. State: <root>/fake.json
//   { env: 'ok'|'missing'|'empty-key', unlock: 'ok'|'locked'|'rejected', serve: 'ok'|'fails', update: 'none'|'available',
//     scenario: '<mock scenario>', config: {KEY: value}, calls: [[...argv]] }
import { readFileSync, writeFileSync, realpathSync, existsSync, mkdirSync, rmSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';

const [root, ...args] = process.argv.slice(2);
const file = join(root, 'fake.json');
const st = existsSync(file) ? JSON.parse(readFileSync(file, 'utf8')) : {};
st.config ||= {}; st.calls ||= [];
const SECRET = /KEY|SECRET|TOKEN|PASSWORD|COOKIE/;
// a call is logged with secret values replaced by their length, as a real log must never hold them
st.calls.push(args.map((a, i) => (i === 3 && args[0] === 'config' && args[1] === 'set' && SECRET.test(args[2]) ? `<${a.length} chars>` : a)));
const save = () => writeFileSync(file, JSON.stringify(st, null, 2));
const out = (s) => process.stdout.write(s.endsWith('\n') ? s : s + '\n');
const err = (s) => process.stderr.write(s.endsWith('\n') ? s : s + '\n');

const KEYS = [
  ['PROXY_API_URL', 'Upstream base URL'], ['PROXY_API_KEY', 'Upstream API key'], ['DEFAULT_MODEL_NAME', 'Default model'],
  ['OAI_API_URL', 'OpenAI-format base URL'], ['OAI_API_KEY', 'OpenAI-format key'], ['OAI_MODELS', 'OpenAI-format models'],
  ['CHATGPT_BASE_URL', 'ChatGPT base URL'], ['CHATGPT_MODEL_NAME', 'ChatGPT model'], ['CHATGPT_COOKIE_STRING', 'ChatGPT cookie'],
  ['TRACKER_TOKEN', 'Ticket tracker token'], ['QUEUE_MODEL', 'Queue model'], ['HTTPS_PROXY', 'Corporate proxy'],
];
const defaults = { PROXY_API_URL: 'http://127.0.0.1:9/v1', PROXY_API_KEY: st.env === 'empty-key' ? '' : 'sk-fake-0000000000001234', DEFAULT_MODEL_NAME: 'mock-model' };
const val = (k) => (k in st.config ? st.config[k] : defaults[k] ?? '');

let code = 0;
const [cmd, sub, ...rest] = args;
if (cmd === 'config' && sub === 'list') {
  if (st.env === 'missing' && !existsSync(join(root, '.env'))) { err(`ERROR: missing ${root}/.env`); code = 1; }
  else for (const [k, d] of KEYS) { const v = val(k); const s = SECRET.test(k); out(`${k}\t${s && v ? `(set, ${v.length} chars)` : v}\t${s ? 1 : 0}\t${d}`); }
} else if (cmd === 'config' && sub === 'set') {
  const [k, v = ''] = rest;
  if (!k || !KEYS.some(([n]) => n === k)) { err(`ERROR: unknown key ${k}`); code = 1; }
  else { st.config[k] = v; if (!existsSync(join(root, '.env'))) writeFileSync(join(root, '.env'), '# fake\n'); out(`[harness] ${k} set`); }
} else if (cmd === 'config' && sub === 'get') {
  out(val(rest[0] || ''));
} else if (cmd === 'unlock') {
  if (st.unlock === 'locked') { err(' ERROR: Upstream API key is locked'); err(' Visit this URL in your browser to unlock the key:'); err('   https://unlock.invalid/k/abc123'); code = 1; }
  else if (st.unlock === 'rejected') { err('ERROR: Upstream rejected the API key (HTTP 401)'); code = 1; }
  else out('[harness] upstream API key is OK; no unlock needed');
} else if (cmd === 'check-updates') {
  if (st.update === 'available') out("[harness] update available on dev; run 'harness upgrade' to update");
  else out('[harness] up to date with origin/dev (abc1234)');
} else if (cmd === 'upgrade') {
  out('[harness] fetching origin/dev'); out('[harness] upgraded to def5678'); st.update = 'none';
} else if (cmd === 'doctor') {
  out('[harness] doctor: python3 ok'); out('[harness] doctor: node ok'); out('[harness] doctor: all checks passed');
} else if (cmd === 'serve' && sub === 'stop') {
  const pf = join(root, 'state', 'fake-serve.pid');
  if (existsSync(pf)) { const pid = Number(readFileSync(pf, 'utf8')); try { process.kill(pid, 'SIGTERM'); } catch { /* gone */ } rmSync(pf, { force: true }); }
} else if (cmd === 'serve' && sub === 'code') {
  if (rest[0] === 'add') out('mock-code-1234'); else out('desktop');
} else if (cmd === 'serve') {
  save();
  if (st.serve === 'fails') { err('[harness] starting serve'); err('ERROR: Upstream rejected the API key (HTTP 401)'); process.exit(1); }
  if (st.unlock === 'locked' && process.env.HARNESS_SKIP_AUTH_PROBE !== '1') {
    err(' ERROR: Upstream API key is locked'); err(' Visit this URL in your browser to unlock the key:'); err('   https://unlock.invalid/k/abc123'); process.exit(1);
  }
  const i = args.indexOf('--port');
  const port = i > 0 ? Number(args[i + 1]) : 8780;
  mkdirSync(join(root, 'state'), { recursive: true });
  const stateId = createHash('sha256').update(realpathSync(join(root, 'state'))).digest('hex').slice(0, 16);
  const { startMock } = await import(join(dirname(fileURLToPath(import.meta.url)), '..', 'mock-serve', 'server.mjs'));
  const m = await startMock({ port, stateId, scenario: st.scenario || 'healthy' });
  writeFileSync(join(root, 'state', 'fake-serve.pid'), String(process.pid));
  out(`[harness] serve on ${m.url}`);
  const bye = () => { m.stop().finally(() => process.exit(0)); };
  process.on('SIGTERM', bye); process.on('SIGINT', bye); process.on('SIGHUP', bye);
  await new Promise(() => {});
} else {
  out(`[harness] fake: ${args.join(' ')}`);
}
save();
process.exit(code);
