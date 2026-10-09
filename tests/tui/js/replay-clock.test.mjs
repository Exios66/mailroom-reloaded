import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createClock, SPEEDS } from '../../../src/mailroom_reloaded/api/tui/replay/clock.js';

test('starts paused at zero', () => {
  const c = createClock({ duration: 10 });
  assert.deepEqual(c.state(), { t: 0, playing: false, speed: 1, duration: 10, ended: false });
  assert.equal(c.tick(5000), 0);
});

test('play advances with caller time, pause freezes', () => {
  const c = createClock({ duration: 100 });
  c.play(1000);
  assert.equal(c.tick(3500), 2.5);
  c.pause(4000);
  assert.equal(c.state().t, 3);
  assert.equal(c.tick(9000), 3);
  assert.equal(c.state().playing, false);
  c.toggle(10000);
  assert.equal(c.state().playing, true);
  assert.equal(c.tick(11000), 4);
});

test('seek and step clamp and keep play state', () => {
  const c = createClock({ duration: 10 });
  assert.equal(c.seek(99), 10);
  assert.equal(c.seek(-5), 0);
  assert.equal(c.step(3), 3);
  assert.equal(c.step(-10), 0);
  c.play(0);
  c.seek(4);
  assert.equal(c.state().playing, true);
  assert.equal(c.tick(100), 4);
  assert.equal(c.tick(1100), 5);
});

test('NaN and non-finite inputs are ignored', () => {
  const c = createClock({ duration: 10 });
  c.seek(2);
  assert.equal(c.seek(NaN), 2);
  assert.equal(c.step(NaN), 2);
  assert.equal(c.step(Infinity), 2);
  c.play(0);
  c.tick(1000);
  assert.equal(c.tick(NaN), 3);
  assert.equal(c.tick(Infinity), 3);
  assert.equal(c.state().t, 3);
});

test('speed set, validation, faster and slower', () => {
  const c = createClock({ duration: 100 });
  assert.equal(c.setSpeed(3), false);
  assert.equal(c.state().speed, 1);
  c.play(0);
  c.tick(1000);
  assert.equal(c.setSpeed(4), true);
  assert.equal(c.state().t, 1);
  assert.equal(c.tick(2000), 5);
  for (let i = 0; i < 10; i++) c.faster();
  assert.equal(c.state().speed, SPEEDS[SPEEDS.length - 1]);
  for (let i = 0; i < 10; i++) c.slower();
  assert.equal(c.state().speed, SPEEDS[0]);
  assert.equal(createClock({ duration: 1, speed: 7 }).state().speed, 1);
  assert.equal(createClock({ duration: 1, speed: 8 }).state().speed, 8);
});

test('setSpeed folds elapsed time at the old speed', () => {
  const c = createClock({ duration: 100 });
  c.play(0);
  c.setSpeed(2, 1000);
  assert.equal(c.state().t, 1);
  assert.equal(c.tick(2000), 3);
});

test('reaching the end pauses, clamps and reports ended', () => {
  const c = createClock({ duration: 2 });
  c.play(0);
  assert.equal(c.tick(5000), 2);
  assert.deepEqual(c.state(), { t: 2, playing: false, speed: 1, duration: 2, ended: true });
  c.play(6000);
  assert.equal(c.state().t, 0);
  assert.equal(c.state().ended, false);
});

test('backwards time never moves t backwards', () => {
  const c = createClock({ duration: 100 });
  c.play(5000);
  assert.equal(c.tick(6000), 1);
  assert.equal(c.tick(2000), 1);
  assert.equal(c.tick(6500), 1.5);
});

test('bad durations become zero', () => {
  for (const d of [NaN, Infinity, -4, undefined, 'x']) {
    const c = createClock({ duration: d });
    assert.equal(c.state().duration, 0);
    assert.equal(c.seek(5), 0);
  }
  const c = createClock({ duration: 0 });
  c.play(0);
  assert.doesNotThrow(() => c.tick(1000));
  assert.equal(c.state().playing, false);
});
