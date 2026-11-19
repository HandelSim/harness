// Mock data for the e2e mock serve: one function per scenario of PLAN 7.5, shaped like src/shared/api.ts.
const now = Date.now();
const ok = { ok: true, reason: '' };
const no = (reason) => ({ ok: false, reason });
const ACTIONS = ['answer', 'approve', 'retry', 'continue', 'stop', 'remove', 'move', 'note', 'followup', 'edit', 'fix', 'archive', 'unarchive'];
const gates = (allowed) => Object.fromEntries(ACTIONS.map((a) => [a, allowed.includes(a) ? ok : no(`not while ${a === 'stop' ? 'idle' : 'in this state'}`)]));

export function status(over = {}) {
  return { api: 1, mode: 'serve', pid: process.pid, state_id: '', stale: false, root: '/fake/harness', state_dir: '/fake/harness/state',
    codes: false, user: null, runs: true, opencode: { port: 0, url: '' }, proxy: { url: 'http://127.0.0.1:9', up: true, detail: 'mock' },
    vault: { name: 'demo', dir: '/fake/vault' }, modes: [{ name: 'container', description: 'in the container' }, { name: 'host', description: 'on the host' }],
    version: { branch: 'dev', sha: 'abc1234', describe: 'mock-1' }, ...over };
}
export function runner(over = {}) {
  return { running: false, starting: false, halting: false, exited: null, ticket: '', stage: '', step: '', model: 'mock-model', started: null,
    since: null, host: false, single: false, watch: false, parallel: 1, on: [], pid: 0, beat: null, ...over };
}
export function ticketSummary(key, st = 'queued', over = {}) {
  const allowed = { queued: ['remove', 'move', 'note', 'edit'], running: ['stop', 'note'], 'needs-answers': ['answer', 'note', 'stop'],
    'plan-review': ['approve', 'note', 'stop'], done: ['followup', 'archive', 'note'], blocked: ['retry', 'continue', 'remove', 'note'],
    interrupted: ['retry', 'continue', 'note'], stopped: ['retry', 'remove', 'note'] }[st] || [];
  return { key, title: `Mock ticket ${key}`, status: st, stage: st === 'running' ? 'implement' : '', step: '', reason: '', created: now - 3600e3,
    updated: now - 60e3, finished: null, attempt: 1, project: 'demo', branch: `harness/${key.toLowerCase()}`, stopping: false, debug: false,
    session: st === 'running' ? { stage: 'implement', id: 'ses_mockrunning01' } : null, actions: gates(allowed), ...over };
}
export function queue(tickets, over = {}) {
  return { runner: runner(), tickets, archived: [], start: ok, ...over };
}
export function sessionRow(id, over = {}) {
  return { id, parent: null, source: 'host', directory: '/fake/project', title: `Session ${id.slice(-4)}`, agent: 'build', model: 'mock-model',
    created: now - 7200e3, updated: now - 120e3, tokens_in: 1200, tokens_out: 300, cost: 0, kids: 0, working: false, live: false, ticket: null,
    kind: 'session', label: 'session', web: null, ...over };
}
export function ticket(key, st = 'queued', over = {}) {
  const s = ticketSummary(key, st);
  return { key, archived: false, actions: s.actions, retry_choices: [{ value: '', label: 'from the failed stage' }],
    state: { status: st, stage: s.stage, title: s.title, reason: '', plan_sig: 'plan-sig-1', criteria: [{ id: 'c1', text: 'It works' }],
      questions: st === 'needs-answers' ? [{ id: 'q1', text: 'Which database?', ask: 'Which database?', blocking: true, default: 'sqlite', options: ['sqlite', 'postgres'] }] : [],
      notes: [], history: [{ at: now - 3000e3, stage: 'plan', attempt: 1, result: 'ok', details: '' }] },
    sessions: s.session ? [{ sid: s.session.id, stage: 'implement', label: 'implement', kind: 'queue', working: true, created: now - 600e3 }] : [],
    outs: [{ name: 'plan.md', mtime: now - 3000e3, size: 120 }], log: `# ${key}\n\nmock log\n`, ...over };
}

function base() {
  const sessions = [sessionRow('ses_mockaaaa0001'), sessionRow('ses_mockbbbb0002', { title: 'Refactor parser' })];
  return {
    status: status(),
    queue: queue([ticketSummary('DEMO-1', 'queued'), ticketSummary('DEMO-2', 'done')]),
    sessions,
    home: { live: [], usage: null, pages: [{ rel: 'index.md', title: 'Index' }], activity: [] },
    findings: { items: [{ id: 'f1', title: 'Unchecked return value', archived: false }], order: ['f1'] },
    'findings-agent': { running: false, status: {}, passes: [], folders: [], last_repos: [], notes: '', knowledge: [] },
    vault: { name: 'demo', dir: '/fake/vault', facts: {}, pages: [{ rel: 'index.md', title: 'Index' }], ingest: { run: null, summary: null, out_tail: '' }, log: [] },
    ingest: { run: null, summary: null, out_tail: '' },
    debug: [],
    'ticket:DEMO-1': ticket('DEMO-1', 'queued'),
    'ticket:DEMO-2': ticket('DEMO-2', 'done'),
    'session:ses_mockaaaa0001': { row: sessions[0], count: 2, children: [], inbox: [], send: no('not live'), stop: no('not working') },
    'session:ses_mockbbbb0002': { row: sessions[1], count: 2, children: [], inbox: [], send: no('not live'), stop: no('not working') },
  };
}

/** Returns { topics, flags } for a scenario name. flags steer server behaviour (codes, stale, oldServe, ...). */
export function scenario(name) {
  const t = base();
  const flags = { codes: false, stale: false, oldServe: false, refuseAll: false, slowMs: 0, flaky: false, changedOn: [] };
  switch (name) {
    case 'healthy': break;
    case 'empty':
      t.queue = queue([]); t.sessions = []; t.findings = { items: [], order: [] }; t.home = { live: [], usage: null, pages: [], activity: [] };
      for (const k of Object.keys(t)) if (k.includes(':')) delete t[k];
      break;
    case 'codes-on': flags.codes = true; t.status = status({ codes: true, user: 'desktop' }); break;
    case 'old-serve': flags.oldServe = true; break;
    case 'incompatible': t.status = status({ api: 2 }); break;
    case 'viewer': t.status = status({ mode: 'viewer', opencode: null }); break;
    case 'stale': flags.stale = true; t.status = status({ stale: true }); break;
    case 'queue-running': {
      const run = ticketSummary('DEMO-3', 'running');
      t.queue = queue([run, ticketSummary('DEMO-1', 'queued')], { runner: runner({ running: true, ticket: 'DEMO-3', stage: 'implement', pid: 4242, started: now - 600e3 }), start: no('a run is already going') });
      t['ticket:DEMO-3'] = ticket('DEMO-3', 'running');
      const row = sessionRow('ses_mockrunning01', { working: true, live: true, ticket: 'DEMO-3', kind: 'queue', label: 'implement' });
      t.sessions = [row, ...t.sessions];
      t['session:ses_mockrunning01'] = { row, count: 3, children: [], inbox: [], send: ok, stop: ok };
      break;
    }
    case 'needs-answers':
      t.queue = queue([ticketSummary('DEMO-4', 'needs-answers'), ticketSummary('DEMO-1', 'queued')]);
      t['ticket:DEMO-4'] = ticket('DEMO-4', 'needs-answers');
      break;
    case 'plan-changed':
      t.queue = queue([ticketSummary('DEMO-5', 'plan-review')]);
      t['ticket:DEMO-5'] = ticket('DEMO-5', 'plan-review');
      flags.changedOn = ['/api/v1/tickets/DEMO-5/approve', '/api/v1/tickets/DEMO-5/answer'];
      break;
    case 'refuse-all': flags.refuseAll = true; break;
    case 'slow-actions': flags.slowMs = 3000; break;
    case 'flaky': flags.flaky = true; break;
    default: throw new Error(`unknown scenario ${name}`);
  }
  return { topics: t, flags };
}

/** GET path (under /api/v1) of a topic. */
export function topicPath(topic) {
  const [name, arg] = topic.split(/:(.*)/s);
  const p = { status: '/status', home: '/home', queue: '/queue', sessions: '/sessions', findings: '/findings', 'findings-agent': '/findings/agent',
    vault: '/vault', ingest: '/vault/ingest', debug: '/debug', ticket: `/tickets/${arg}`, session: `/sessions/${arg}` }[name];
  return p || `/${name}`;
}
