// Web-version paths must be GET routes serve/pub.py answers (do_GET, runs_get, vault_get); review 2 part 3 item 1.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { VIEW_WEB, WEB } from './panels';

// What do_GET routes: '/', '/runs', '/runs/...', '/vault', '/vault/...', '/settings', '/pub/...'.
const routed = (p: string) => p === '/' || p === '/runs' || p.startsWith('/runs/') || p === '/vault' || p.startsWith('/vault/')
  || p === '/settings' || p.startsWith('/pub/');
const pathOnly = (p: string) => p.split('#')[0].split('?')[0];

test('every view falls back to a page serve routes', () => {
  for (const [id, p] of Object.entries(VIEW_WEB)) assert.ok(routed(pathOnly(p)), `${id}: ${p}`);
  // the dead ones from review 2: /sessions, /jira, /debug, JSON /runs/queue/jira, POST-only /settings/system, /runs/findings
  for (const p of Object.values(VIEW_WEB)) {
    assert.ok(!['/runs/queue/jira', '/settings/system', '/runs/findings'].includes(pathOnly(p)), p);
  }
  assert.equal(VIEW_WEB.jira, '/runs/queue#jira');
  assert.equal(VIEW_WEB.findings, '/runs/queue#findings');
});

test('per-item web pages', () => {
  assert.equal(WEB.session('ses_a.b-1'), '/runs/ses_a.b-1');
  assert.equal(WEB.debugReport('runs', '20261030-120000-ABC-12'), '/runs/debug/runs/20261030-120000-ABC-12');
  assert.equal(WEB.debugReport('reports', 'a b#.md'), '/runs/debug/reports/a%20b%23.md');
  assert.equal(WEB.vaultFile('notes/a b/c#?.md'), '/vault/browse/notes/a%20b/c%23%3F.md');
  for (const p of [WEB.findingsAgent, WEB.findingsNotes]) assert.ok(routed(pathOnly(p)), p);
});
