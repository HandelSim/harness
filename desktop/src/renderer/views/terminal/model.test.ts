import { test } from 'node:test';
import assert from 'node:assert/strict';
import { specTitle, baseName, pasteLines, pasteNeedsConfirm, WebglSlots, isLaunchFailure, exitText, PendingBuffer, canTryHost, unclaimedPtys } from './model';

test('baseName and specTitle', () => {
  assert.equal(baseName('/home/u/proj/'), 'proj');
  assert.equal(baseName('C:\\work\\app'), 'app');
  assert.equal(baseName('/'), '/');
  assert.equal(specTitle({ kind: 'agent', cwd: '/x/repo' }), '◆ repo');
  assert.equal(specTitle({ kind: 'host', cwd: '/x/repo', ticket: 'AB-1' }), '◇ repo AB-1');
});

test('paste confirmation', () => {
  assert.equal(pasteLines(''), 0);
  assert.equal(pasteLines('a'), 1);
  assert.equal(pasteLines('a\nb\n'), 2);
  assert.equal(pasteLines('a\r\nb\r\nc'), 3);
  assert.equal(pasteNeedsConfirm('ls', true, false), false);
  assert.equal(pasteNeedsConfirm('ls\n', true, false), true);
  assert.equal(pasteNeedsConfirm('a\nb', false, false), false);
  assert.equal(pasteNeedsConfirm('a\nb', true, true), false);
});

test('webgl slots cap at 8', () => {
  const s = new WebglSlots();
  for (let i = 0; i < 8; i++) assert.equal(s.acquire(`t${i}`), true);
  assert.equal(s.acquire('t8'), false);
  assert.equal(s.acquire('t3'), true, 're-acquire is idempotent');
  s.release('t0');
  assert.equal(s.acquire('t8'), true);
  assert.equal(s.size, 8);
});

test('exit classification', () => {
  assert.equal(isLaunchFailure(0, 4000, { code: 1, signal: 0 }), true);
  assert.equal(isLaunchFailure(0, 6000, { code: 1, signal: 0 }), false);
  assert.equal(isLaunchFailure(0, 1000, { code: 0, signal: 0 }), false);
  assert.equal(exitText({ code: 2, signal: 0 }), 'Process exited (code 2)');
  assert.equal(exitText({ code: 0, signal: 9 }), 'Process exited (signal 9)');
  assert.equal(canTryHost({ kind: 'agent', cwd: '/' }), true);
  assert.equal(canTryHost({ kind: 'plain', cwd: '/' }), false);
});

test('pending buffer is bounded and expires', () => {
  const b = new PendingBuffer(10, 100);
  b.push('a', 'hello', 0);
  b.push('a', 'world', 1);
  b.push('a', '!', 2);           // would exceed 10 chars: dropped
  assert.deepEqual(b.take('a'), ['hello', 'world']);
  assert.deepEqual(b.take('a'), []);
  b.push('x', 'y', 0);
  b.push('z', 'w', 500);         // gc drops x (older than ttl)
  assert.deepEqual(b.ids, ['z']);
});

test('unclaimedPtys (H3): kills old ptys no restored panel claims, keeps claimed and newly started ones', () => {
  const info = (id: string, startedAt: number) => ({ id, spec: { kind: 'shell' } as never, pid: 1, startedAt, exited: null });
  const list = [info('a', 100), info('b', 100), info('c', 900), info('d', 100)];
  assert.deepEqual(unclaimedPtys(list, ['b', undefined, 7], 500), ['a', 'd']);
  assert.deepEqual(unclaimedPtys([], ['x'], 500), []);
});
