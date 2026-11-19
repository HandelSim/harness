import { test } from 'node:test';
import assert from 'node:assert/strict';
import { restartConsequences, upgradeConsequences } from './consequences';
import type { AppBusy } from '../../../shared/ipc';

const b = (o: Partial<AppBusy>): AppBusy => ({ terminals: 0, queueRunning: false, ownedServe: false, onQuit: 'keep-if-busy', jobs: [], ...o });

test('restart lists open terminals, a running queue and running jobs (W15)', () => {
  const r = restartConsequences(b({ terminals: 2, queueRunning: true, ownedServe: true, onQuit: 'stop', jobs: ['upgrade'] }));
  assert.equal(r.length, 3);
  assert.match(r[0], /2 open terminals are closed/);
  assert.match(r[1], /stops the backend/);
  assert.match(r[2], /upgrade/);
  assert.match(restartConsequences(b({ terminals: 1 }))[0], /1 open terminal is closed/);
  assert.deepEqual(restartConsequences(b({})), ['Nothing is running that the restart would interrupt.']);
  assert.match(restartConsequences(null)[0], /Could not check/);
});

test('upgrade names serve restart and a running queue run (W15)', () => {
  assert.equal(upgradeConsequences(b({})).length, 1);
  const u = upgradeConsequences(b({ queueRunning: true }));
  assert.match(u[1], /restarts between tickets/);
  assert.match(upgradeConsequences(null)[1], /Could not check/);
});
