// review2 P1-6: background.onBattery was a dead control; it now drives the engine run state.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { bgRunState } from './runstate';

test('runstate: hidden pauses; focus runs; blur follows onBlur', () => {
  assert.equal(bgRunState({ visible: false, focused: true }, 'run', false, 'run'), 'pause');
  assert.equal(bgRunState({ visible: true, focused: true }, 'dim', false, 'static'), 'run');
  assert.equal(bgRunState({ visible: true, focused: false }, 'dim', false, 'static'), 'dim');
  assert.equal(bgRunState({ visible: true, focused: false }, 'run', false, 'static'), 'run');
});
test('runstate: on battery, static stops the animation; run keeps it', () => {
  assert.equal(bgRunState({ visible: true, focused: true }, 'run', true, 'static'), 'pause');
  assert.equal(bgRunState({ visible: true, focused: true }, 'run', true, 'run'), 'run');
  assert.equal(bgRunState({ visible: true, focused: false }, 'dim', true, 'run'), 'dim');
});
