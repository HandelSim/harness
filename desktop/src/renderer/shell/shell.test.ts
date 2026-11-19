import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseChord, Keymap, mergeBindings, formatChord, type KeyLike } from '../core/keymap';
import { panelId, parsePanelId, isKeyed, validateParams, ReopenStack, SnapshotStack } from './panels';
import { onboardingStep } from './onboarding-model';
import type { BackendState } from '../../shared/ipc';

const ev = (key: string, m: Partial<KeyLike> = {}): KeyLike => ({ key, ctrlKey: false, metaKey: false, altKey: false, shiftKey: false, ...m });

test('parseChord: Mod maps per platform, Mod++ is the plus key, two keys is invalid', () => {
  assert.deepEqual(parseChord('Mod+K', 'linux'), { ctrl: true, meta: false, alt: false, shift: false, key: 'k' });
  assert.equal(parseChord('Mod+K', 'darwin')?.meta, true);
  assert.equal(parseChord('Mod++', 'linux')?.key, '+');
  assert.equal(parseChord('A+B', 'linux'), null);
  assert.equal(parseChord('Ctrl+Shift', 'linux'), null);
  assert.ok(formatChord('Mod+K', 'linux').length > 0);
});

test('Keymap lookup, list chords and conflicts', () => {
  const km = new Keymap('linux', [{ command: 'palette.open', chord: 'Mod+K, Ctrl+Shift+P' }, { command: 'x', chord: 'Mod+K' }]);
  assert.deepEqual(km.lookup(ev('k', { ctrlKey: true })), ['palette.open', 'x']);
  assert.deepEqual(km.lookup(ev('P', { ctrlKey: true, shiftKey: true })), ['palette.open']);
  assert.equal(km.conflicts().length, 1);
});

test('W18 terminal rule: Ctrl+<key> goes to the pty, Ctrl+Shift+<key> reaches the app, copy/paste stay in the terminal', () => {
  const km = new Keymap('linux', [{ command: 'palette.open', chord: 'Mod+K' }, { command: 'tab.close', chord: 'Mod+W' }, { command: 'view.home', chord: 'Alt+1' }, { command: 'f', chord: 'F5' }]);
  assert.deepEqual(km.lookupInTerminal(ev('k', { ctrlKey: true })), []);
  assert.deepEqual(km.lookupInTerminal(ev('K', { ctrlKey: true, shiftKey: true })), ['palette.open']);
  assert.deepEqual(km.lookupInTerminal(ev('C', { ctrlKey: true, shiftKey: true })), []);
  assert.deepEqual(km.lookupInTerminal(ev('V', { ctrlKey: true, shiftKey: true })), []);
  assert.deepEqual(km.lookupInTerminal(ev('1', { altKey: true, code: 'Digit1' })), ['view.home']);
  assert.deepEqual(km.lookupInTerminal(ev('F5')), ['f']);
  assert.deepEqual(km.lookupInTerminal(ev('a')), []);
  const mac = new Keymap('darwin', [{ command: 'palette.open', chord: 'Mod+K' }]);
  assert.deepEqual(mac.lookupInTerminal(ev('k', { metaKey: true })), ['palette.open']);
  assert.deepEqual(mac.lookupInTerminal(ev('k', { ctrlKey: true })), []);
});

test('mergeBindings: override replaces, empty unbinds, new commands are added', () => {
  const d = [{ command: 'a', chord: 'Mod+A' }, { command: 'b', chord: 'Mod+B' }];
  assert.deepEqual(mergeBindings(d, { a: 'Mod+Shift+A', b: '', c: 'Mod+C' }), [{ command: 'a', chord: 'Mod+Shift+A' }, { command: 'c', chord: 'Mod+C' }]);
});

test('panel ids round-trip and restored params are validated (W20)', () => {
  assert.equal(panelId('view', 'queue'), 'view.queue');
  assert.deepEqual(parsePanelId('ticket:ABC-1'), { kind: 'ticket', key: 'ABC-1' });
  assert.deepEqual(parsePanelId(panelId('terminal', 'x1')), { kind: 'terminal', key: 'x1' });
  assert.equal(parsePanelId('view.nope'), null);
  assert.equal(parsePanelId('bogus:1'), null);
  assert.equal(parsePanelId('ticket:'), null);
  assert.equal(isKeyed('ticket'), true);
  assert.equal(isKeyed('terminal'), false);
  assert.ok(validateParams('ticket', { key: 'ABC-1' }));
  assert.equal(validateParams('ticket', { key: '../etc' }), null);
  assert.equal(validateParams('web', { path: '//evil.example' }), null);
  assert.ok(validateParams('web', { path: '/runs/queue' }));
  assert.equal(validateParams('terminal', { spec: { kind: 'rm', cwd: '/' } }), null);
  assert.ok(validateParams('terminal', { spec: { kind: 'shell', cwd: '/home' } }));
  assert.equal(validateParams('transcript', null), null);
});

test('reopen stack is capped; snapshot undo skips duplicates', () => {
  const r = new ReopenStack<number>([], 3);
  for (let i = 0; i < 5; i++) r.push(i);
  assert.equal(r.size, 3);
  assert.equal(r.pop(), 4);
  const s = new SnapshotStack(5);
  s.push('a'); s.push('a'); s.push('b'); s.push('c');
  assert.equal(s.undo(), 'b');
  assert.equal(s.undo(), 'a');
  assert.equal(s.undo(), undefined);
});

test('onboarding step from backend phase', () => {
  const b = (phase: BackendState['phase']) => ({ phase } as BackendState);
  assert.equal(onboardingStep(null, null), null);
  assert.equal(onboardingStep(b('no-install'), null), 1);
  assert.equal(onboardingStep(b('needs-config'), null), 2);
  assert.equal(onboardingStep(b('needs-login'), null), 3);
  assert.equal(onboardingStep(b('incompatible'), null), 3);
  assert.equal(onboardingStep(b('up'), null), null);
  assert.equal(onboardingStep(b('reconnecting'), null), null);
});
