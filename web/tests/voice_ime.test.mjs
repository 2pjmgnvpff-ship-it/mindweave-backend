import { test } from 'node:test';
import assert from 'node:assert/strict';
import { shouldSendOnEnter, citationLabel, formatDateCn } from '../scripts/format.js';
import {
  computeZone,
  routeFor,
  decideRelease,
  formatTimer,
  shouldAutoStop,
  pickMimeType,
  extensionFor,
  levelFromTimeDomain,
  MIN_HOLD_MS,
  MAX_HOLD_MS,
} from '../scripts/voice-logic.js';

test('IME guard: Enter sends only when not composing and no Shift', () => {
  assert.equal(shouldSendOnEnter({ key: 'Enter' }), true);
  assert.equal(shouldSendOnEnter({ key: 'Enter', isComposing: true }), false);
  assert.equal(shouldSendOnEnter({ key: 'Enter', keyCode: 229 }), false);
  assert.equal(shouldSendOnEnter({ key: 'Enter', shiftKey: true }), false);
  assert.equal(shouldSendOnEnter({ key: 'a' }), false);
  assert.equal(shouldSendOnEnter(null), false);
});

test('citation label never shows an id and uses Chinese date', () => {
  const label = citationLabel({ materialId: '3f0c-uuid', title: '膝盖疼', happenedOn: '2026-05-12' });
  assert.equal(label, '膝盖疼 · 5月12日');
  assert.ok(!label.includes('3f0c'));
  assert.equal(citationLabel({ materialId: 'abc' }), '一页手帐');
  assert.equal(formatDateCn('nope'), '');
});

test('voice min-hold: under 300ms is discarded, nothing written', () => {
  assert.equal(decideRelease({ zone: 'current', durationMs: 120 }), 'too-short');
  assert.equal(decideRelease({ zone: 'global', durationMs: MIN_HOLD_MS - 1 }), 'too-short');
  assert.equal(decideRelease({ zone: 'current', durationMs: MIN_HOLD_MS }), 'submit');
  assert.equal(decideRelease({ zone: 'cancel', durationMs: 5000 }), 'cancel');
  assert.equal(decideRelease({ zone: 'current', durationMs: NaN }), 'too-short');
});

test('voice zones: up = global, right/default = current, far = cancel', () => {
  assert.equal(computeZone(0, 0), 'current');
  assert.equal(computeZone(10, 5), 'current');
  assert.equal(computeZone(0, 120), 'global');
  assert.equal(computeZone(-30, 100), 'global');
  assert.equal(computeZone(120, 20), 'current');
  assert.equal(computeZone(160, 160), 'cancel');
  assert.equal(computeZone(0, 200), 'cancel');
});

test('directory view defaults to global and never files to an arbitrary person', () => {
  assert.equal(computeZone(0, 0, { directory: true }), 'global');
  assert.equal(computeZone(120, 10, { directory: true }), 'global');
  assert.deepEqual(routeFor('current', { activePersonId: 'p1', directory: true }), { routeMode: 'global', activePersonId: null });
  assert.deepEqual(routeFor('current', { activePersonId: 'p1' }), { routeMode: 'current', activePersonId: 'p1' });
  assert.deepEqual(routeFor('current', {}), { routeMode: 'global', activePersonId: null });
  assert.equal(routeFor('cancel', { activePersonId: 'p1' }), null);
});

test('timer supports more than 59 seconds and caps at 120s', () => {
  assert.equal(formatTimer(7000), '0:07');
  assert.equal(formatTimer(65000), '1:05');
  assert.equal(formatTimer(MAX_HOLD_MS), '2:00');
  assert.equal(shouldAutoStop(119999), false);
  assert.equal(shouldAutoStop(120000), true);
});

test('recorder mime type falls back to mp4 on Safari', () => {
  assert.equal(pickMimeType((t) => t.startsWith('audio/webm')), 'audio/webm;codecs=opus');
  assert.equal(pickMimeType((t) => t === 'audio/mp4'), 'audio/mp4');
  assert.equal(pickMimeType(() => false), '');
  assert.equal(pickMimeType(undefined), '');
  assert.equal(extensionFor('audio/mp4'), 'm4a');
  assert.equal(extensionFor('audio/webm;codecs=opus'), 'webm');
});

test('analyser level is 0 for silence and grows with amplitude', () => {
  assert.equal(levelFromTimeDomain(new Uint8Array(64).fill(128)), 0);
  const loud = new Uint8Array(64).map((_, i) => (i % 2 ? 200 : 56));
  assert.ok(levelFromTimeDomain(loud) > 0.5);
});
