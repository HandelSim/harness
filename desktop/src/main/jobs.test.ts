import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Jobs, runHarness } from './cli';

test('M3: one running job per kind; a second request re-attaches; the ring keeps the output tail', async () => {
  const exits: ((c: number) => void)[] = [];
  const chunks: ((s: string) => void)[] = [];
  const events: [string, unknown][] = [];
  const done: string[] = [];
  const j = new Jobs({ start: (_r, _a, onChunk, onExit) => { chunks.push(onChunk); exits.push(onExit); },
    emit: (ch, p) => events.push([ch, p]), done: (job, out) => done.push(`${job}:${out}`) });
  const a = j.run('upgrade', '/r');
  const b = j.run('upgrade', '/r');
  assert.equal(a.id, b.id);
  await new Promise((r) => setTimeout(r, 5));
  assert.equal(exits.length, 1, 'only one process started');
  const c = j.run('doctor', '/r');
  assert.notEqual(c.id, a.id);
  await new Promise((r) => setTimeout(r, 5));
  chunks[0]('fetching\n');
  assert.deepEqual(j.running().sort(), ['doctor', 'upgrade']);
  assert.equal(j.list().find((x) => x.id === a.id)?.output, 'fetching\n');
  exits[0](0);
  assert.deepEqual(j.running(), ['doctor']);
  assert.deepEqual(done, ['upgrade:fetching\n']);
  const d = j.run('upgrade', '/r');
  assert.notEqual(d.id, a.id);
  const none = j.run('doctor', null);              // the running doctor wins over a no-install request
  assert.equal(none.id, c.id);
  assert.ok(events.some(([ch, p]) => ch === 'job:exit' && (p as { id: string }).id === a.id));
});

test('L9: a runHarness timeout kills the whole group and resolves even if a grandchild ignores SIGTERM', { timeout: 20000 }, async () => {
  const root = mkdtempSync(join(tmpdir(), 'w1a-cli-'));
  try {
    writeFileSync(join(root, 'harness'), `#!/usr/bin/env bash\n(trap '' TERM; echo $BASHPID > "${root}/gc.pid"; exec sleep 30) &\nwait\n`, { mode: 0o755 });
    const t0 = Date.now();
    const r = await runHarness(root, [], { timeoutMs: 400, killGraceMs: 300 });
    assert.equal(r.timedOut, true);
    assert.ok(Date.now() - t0 < 5000, `took ${Date.now() - t0} ms`);
    const gc = Number(readFileSync(join(root, 'gc.pid'), 'utf8'));
    await new Promise((res) => setTimeout(res, 200));
    assert.throws(() => process.kill(gc, 0), 'grandchild is gone');
  } finally { rmSync(root, { recursive: true, force: true }); }
});
