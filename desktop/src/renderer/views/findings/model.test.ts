import { test } from 'node:test';
import assert from 'node:assert/strict';
import type { Finding, FindingsAgent } from '../../../shared/api';
import { agentView, board, copyText, extraFolders, facets, filterFindings, findingView, folderChoices, groupKnowledge, moveTarget, notePath, OPEN_CAP } from './model';

const F = (o: Record<string, unknown>): Finding => ({ archived: false, ...o } as unknown as Finding);

test('findingView reads bug fields, rolling items and builds the search text', () => {
  const v = findingView(F({ id: 'f1', title: 'Crash on save', src: 'DEMO-1', key: 'DEMO-1', cat: 'bug', sev: 'high', path: 'a.py', line: 3,
    body: 'It crashes', steps: ['open', 'save', 7], expected: 'saved', actual: 'crash', trace: [{ path: 'a.py', line: 3, code: 'x()' }, 'junk'] }));
  assert.equal(v.src, 'DEMO-1');
  assert.deepEqual(v.bug?.steps, ['open', 'save', '7']);
  assert.deepEqual(v.bug?.trace, ['a.py:3  x()']);
  assert.ok(v.hay.includes('crash on save') && v.hay.includes('a.py'));
  assert.equal(copyText(v), '- [high] Crash on save (a.py:3): It crashes');
  const r = findingView(F({ id: 'r1', title: 'Unused files', kind: 'rolling', items: [{ id: 'i1', path: 'x.ts', note: 'never imported' }, { path: 'noid' }] }));
  assert.deepEqual(r.rolling.map((i) => i.id), ['i1']);
  assert.equal(r.bug, null);
  assert.equal(r.src, 'agent');
  assert.equal(copyText(findingView(F({ id: 'm', title: 't', md: '- the md line' }))), '- the md line');
  assert.equal(findingView(F({ id: 'u' })).title, '(untitled)');
});

test('board splits open and archived, drops rows without an id and caps open', () => {
  const items = [F({ id: 'a', title: 'A' }), F({ id: 'b', title: 'B', archived: true }), F({ title: 'no id' }), null as unknown as Finding];
  const b = board(items);
  assert.deepEqual(b.open.map((x) => x.id), ['a']);
  assert.deepEqual(b.archived.map((x) => x.id), ['b']);
  const many = board(Array.from({ length: OPEN_CAP + 5 }, (_, i) => F({ id: `f${i}` })));
  assert.equal(many.open.length, OPEN_CAP); assert.equal(many.openTotal, OPEN_CAP + 5);
  assert.deepEqual(board(undefined).open, []);
});

test('filterFindings and facets', () => {
  const rows = [F({ id: '1', title: 'Null deref', src: 'agent', cat: 'bug' }), F({ id: '2', title: 'Dead code', src: 'T-2', key: 'T-2', cat: 'cleanup' }), F({ id: '3', title: 'Other', src: 'A-1', cat: 'bug' })].map(findingView);
  assert.deepEqual(filterFindings(rows, { q: 'dead', src: '', cat: '' }).map((r) => r.id), ['2']);
  assert.deepEqual(filterFindings(rows, { q: '', src: 'agent', cat: '' }).map((r) => r.id), ['1']);
  assert.deepEqual(filterFindings(rows, { q: '', src: '', cat: 'bug' }).map((r) => r.id), ['1', '3']);
  assert.equal(filterFindings(rows, { q: '', src: '', cat: '' }), rows, 'no filter: the same array');
  assert.deepEqual(facets(rows), { sources: ['agent', 'A-1', 'T-2'], cats: ['bug', 'cleanup'] });
});

test('moveTarget gives the before id, "" for the bottom, null when it cannot move', () => {
  const ids = ['a', 'b', 'c', 'd'];
  assert.equal(moveTarget(ids, 'a', -1), null);
  assert.equal(moveTarget(ids, 'b', -1), 'a');
  assert.equal(moveTarget(ids, 'a', 1), 'c');
  assert.equal(moveTarget(ids, 'c', 1), '');
  assert.equal(moveTarget(ids, 'd', 1), null);
  assert.equal(moveTarget(ids, 'zz', 1), null);
});

const A = (o: Partial<FindingsAgent> & { heartbeat?: number | null }): FindingsAgent => ({ running: false, status: {}, passes: [], folders: [], last_repos: [], notes: '', knowledge: [], ...o } as FindingsAgent);

test('agentView labels the state like the web card', () => {
  const now = 10_000;
  assert.equal(agentView(null, now), null);
  assert.equal(agentView(A({}), now)!.label, 'never started');
  const live = agentView(A({ running: true, heartbeat: now - 10, status: { state: 'exploring', model: 'm1', unit: 'repo: src/', session: 'ses_abc', open: 4, counts: { cleanup: { calls: 2 }, bug: { calls: 3, accepted: 1, rejected: 2, why: { dup: 1, vague: 2 } } } },
    passes: [{ outcome: 'found', repo: '/r/proj/', area: 'src', lens: 'bug', session: 'ses_x1', found: 2, started: '10:00' }, { outcome: 'weird', session: 'bad id!' }] }), now)!;
  assert.equal(live.label, 'exploring'); assert.equal(live.tone, 'info'); assert.equal(live.live, true);
  assert.deepEqual(live.info.map(([k]) => k), ['model', 'heartbeat', 'on', 'session', 'open findings']);
  assert.deepEqual(live.counts.map((c) => c.lens), ['bug', 'cleanup']);
  assert.equal(live.counts[0].why, 'vague 2, dup 1');
  assert.deepEqual(live.passes.map((p) => [p.label, p.session, p.tone]), [['proj: src/ (bug)', 'ses_x1', 'ok'], [': / ()', '', 'default']]);
  assert.equal(live.hasPastSession, true);
  assert.equal(agentView(A({ status: { state: 'stopped' }, heartbeat: now - 1000 }), now)!.label, 'stopped');
  const dead = agentView(A({ status: { state: 'exploring' }, heartbeat: now - 600 }), now)!;
  assert.equal(dead.tone, 'err');
  assert.equal(dead.label, 'not answering (last: exploring, heartbeat 10 min ago)');
  assert.ok(dead.info.some(([k, v]) => k === 'heartbeat' && v.endsWith('(stale)')));
});

test('folderChoices and extraFolders', () => {
  assert.deepEqual(folderChoices(A({ folders: ['/a'], last_repos: ['/a', '/b'] })), [{ dir: '/a', on: true }, { dir: '/b', on: false }]);
  assert.deepEqual(folderChoices(A({ folders: 'x', last_repos: ['/b'] })), [{ dir: '/b', on: true }]);
  assert.deepEqual(extraFolders(' /x \n\n/a\n/x\r\n/y', ['/a']), ['/x', '/y']);
});

test('groupKnowledge groups by repo, filters and drops unsafe names', () => {
  const k = [{ slug: 'proj-1', name: 'src.md', repo: '/r/proj', area: 'src', title: 'Source' }, { slug: 'proj-1', name: 'api.md', repo: '/r/proj', area: 'api' },
    { slug: '../x', name: 'a.md' }, { slug: 'other', name: 'b.md', repo: '/r/aaa', area: 'b', summary: 'parser notes' }];
  const g = groupKnowledge(k);
  assert.deepEqual(g.map((x) => [x.repo, x.notes.map((n) => n.name)]), [['aaa', ['b.md']], ['proj', ['api.md', 'src.md']]]);
  assert.deepEqual(groupKnowledge(k, 'parser').map((x) => x.repo), ['aaa']);
  assert.equal(notePath({ slug: 'a b', name: 'c.md' }), '/findings/notes/a%20b/c.md');
  assert.deepEqual(groupKnowledge('nope'), []);
});
