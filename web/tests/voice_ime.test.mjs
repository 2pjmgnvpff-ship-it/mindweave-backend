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
  isSlideUpCancel,
  CANCEL_DY,
  MIN_HOLD_MS,
  MAX_HOLD_MS,
} from '../scripts/voice-logic.js';
import { getSolarTermForMonth, solarTermOnDate, formatLunarDateCn } from '../scripts/lib/solar-terms.js';
import { createImeGuard } from '../scripts/lib/ime-guard.js';

test('IME guard: Enter sends only when not composing and no Shift', () => {
  assert.equal(shouldSendOnEnter({ key: 'Enter' }), true);
  assert.equal(shouldSendOnEnter({ key: 'Enter', isComposing: true }), false);
  assert.equal(shouldSendOnEnter({ key: 'Enter', keyCode: 229 }), false);
  assert.equal(shouldSendOnEnter({ key: 'Enter', shiftKey: true }), false);
  assert.equal(shouldSendOnEnter({ key: 'a' }), false);
  assert.equal(shouldSendOnEnter(null), false);
});

test('lib/ime-guard.js blocks 120ms post-compositionend Enter tail and touch Return', () => {
  const listeners = {};
  const fakeTa = { addEventListener: (t, fn) => (listeners[t] = fn), removeEventListener: () => {} };
  let sends = 0;
  const guard = createImeGuard(fakeTa, { onSubmit: () => (sends += 1), isTouchDevice: false });
  listeners.compositionstart();
  assert.equal(guard.isBlocked(), true);
  listeners.keydown({ key: 'Enter', isComposing: true, preventDefault() {} });
  assert.equal(sends, 0);
  listeners.compositionend();
  // Immediate Enter right after compositionend (< 120ms Safari WebKit tail) must NOT send!
  listeners.keydown({ key: 'Enter', isComposing: false, keyCode: 13, preventDefault() {} });
  assert.equal(sends, 0);
  guard.destroy();
});

test('lib/solar-terms.js matches HKO 2026-2027 solar-term dates & formats Chinese lunar dates', () => {
  assert.equal(solarTermOnDate('2026-02-04'), '立春');
  assert.equal(solarTermOnDate('2026-06-21'), '夏至');
  assert.equal(solarTermOnDate('2026-10-23'), '霜降');
  assert.equal(solarTermOnDate('2026-12-22'), '冬至');
  assert.equal(solarTermOnDate('2027-02-04'), '立春');
  assert.equal(getSolarTermForMonth(2026, 11), '立冬');
  const lunar = formatLunarDateCn(new Date('2026-06-26T00:00:00Z'));
  assert.ok(lunar.startsWith('农历'));
});

test('citation label never shows an id and uses Chinese date', () => {
  const label = citationLabel({ materialId: '3f0c-uuid', title: '膝盖疼', happenedOn: '2026-05-12' });
  assert.equal(label, '膝盖疼 · 5月12日');
  assert.ok(!label.includes('3f0c'));
  assert.equal(citationLabel({ materialId: 'abc' }), '一页手帐');
  assert.equal(formatDateCn('nope'), '');
});

test('voice min-hold & single slide-up cancel (-72px): under 300ms discarded, dy < -72 cancels', () => {
  assert.equal(CANCEL_DY, -72);
  assert.equal(isSlideUpCancel(-80), true);
  assert.equal(isSlideUpCancel(-40), false);
  assert.equal(decideRelease({ zone: 'current', durationMs: 120 }), 'too-short');
  assert.equal(decideRelease({ zone: 'global', durationMs: MIN_HOLD_MS - 1 }), 'too-short');
  assert.equal(decideRelease({ zone: 'current', durationMs: MIN_HOLD_MS }), 'submit');
  assert.equal(decideRelease({ zone: 'cancel', durationMs: 5000 }), 'cancel');
  assert.equal(decideRelease({ zone: 'current', durationMs: NaN }), 'too-short');
});

test('voice zones: singleCancel defaults clean & routeFor preserves global vs current', () => {
  assert.equal(computeZone(0, 0), 'current');
  assert.equal(computeZone(0, 80, { singleCancel: true }), 'cancel');
  assert.deepEqual(routeFor('current', { activePersonId: 'p1', directory: true }), { routeMode: 'global', activePersonId: null });
  assert.deepEqual(routeFor('current', { activePersonId: 'p1' }), { routeMode: 'current', activePersonId: 'p1' });
  assert.deepEqual(routeFor('current', {}), { routeMode: 'global', activePersonId: null });
  assert.equal(routeFor('cancel', { activePersonId: 'p1' }), null);
});

test('voice-fsm.js enforces strict B4 single-session mutex and playback preemption', async () => {
  const { voiceFsm } = await import('../scripts/voice-fsm.js');
  const pb = voiceFsm.acquire('playback');
  assert.ok(pb > 0);
  assert.equal(voiceFsm.getState().activeSource, 'playback');
  // Second playback blocked while active
  assert.equal(voiceFsm.acquire('playback'), null);
  voiceFsm.releasePlayback();
  assert.equal(voiceFsm.getState().activeSource, 'none');
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
