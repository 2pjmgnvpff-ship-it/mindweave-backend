// voice.js — Inline porcelain #voiceInlineCapsule driven by voice-fsm.js (VoiceSessionArbiter).
// 32px Living Orb + live mwSpeech Chinese captions + ONE clean Slide UP (-72px) = CANCEL gesture.
// ZERO fullscreen .voice-hold-stage, ZERO --voice-level CSS thrash, ZERO mwAudio when native!
import { $, h, clear } from '../dom.js';
import { store } from '../store.js';
import { haptic, hapticPrepare, isNativeSpeechAvailable } from '../native-bridge.js';
import {
  PRE_ROLL_MS,
  CAPSULE_REVEAL_MS,
  MIN_HOLD_MS,
  MAX_HOLD_MS,
  isSlideUpCancel,
  decideRelease,
  formatTimer,
} from '../voice-logic.js';
import { voiceFsm } from '../voice-fsm.js';
import { createOrb, setOrbState, driveOrbRms } from '../lib/orb.js';
import { submitVoice, submitText } from './capture.js';
import { whisper, showToast } from './toast.js';
import { actions } from '../actions.js';

const NEAR_CAP_MS = MAX_HOLD_MS - 10_000;

let capsuleEl = null;
let orbEl = null;
let captionEl = null;
let timerEl = null;
let cancelHintEl = null;
let hintTimer = 0;
let hold = null;

function activePerson() {
  const s = store.get();
  return s.activePersonId ? s.people[s.activePersonId] || null : null;
}

function ensureInlineCapsule() {
  if (capsuleEl && capsuleEl.isConnected && orbEl) return capsuleEl;
  capsuleEl = $('#voiceInlineCapsule');
  if (!capsuleEl) {
    capsuleEl = h('div', { class: 'voice-inline-capsule', id: 'voiceInlineCapsule' });
    ($('#app') || document.body).appendChild(capsuleEl);
  }
  clear(capsuleEl);
  orbEl = createOrb({ size: 'voice', state: 'listening' });
  captionEl = h('p', { class: 'voice-inline-caption', text: '正在听…' });
  timerEl = h('span', { class: 'voice-inline-timer', text: '0:00' });
  cancelHintEl = h('span', { class: 'voice-inline-hint', text: '上滑取消' });
  const waves = h('span', { class: 'voice-wave-bars', 'aria-hidden': 'true' }, h('i'), h('i'), h('i'), h('i'), h('i'));
  capsuleEl.setAttribute('role', 'status');
  capsuleEl.setAttribute('aria-live', 'polite');
  capsuleEl.style.display = 'none';
  capsuleEl.append(
    orbEl,
    h('div', { class: 'voice-inline-body' }, captionEl, h('div', { class: 'voice-inline-meta' }, timerEl, cancelHintEl)),
    waves,
  );
  return capsuleEl;
}

function showCapsule() {
  const el = ensureInlineCapsule();
  el.classList.remove('is-cancelling', 'is-near-cap');
  el.style.display = 'flex';
  setOrbState(orbEl, 'listening');
  requestAnimationFrame(() => el.classList.add('is-open'));
}

function hideCapsule() {
  if (!capsuleEl) return;
  capsuleEl.classList.remove('is-open', 'is-cancelling', 'is-near-cap');
  setOrbState(orbEl, 'idle');
  driveOrbRms(orbEl, 0);
  setTimeout(() => {
    if (!hold && capsuleEl) capsuleEl.style.display = 'none';
  }, 200);
}

export function showHoldHint() {
  const hint = $('#holdHint');
  if (hint) {
    hint.classList.add('is-visible');
    clearTimeout(hintTimer);
    hintTimer = setTimeout(() => hint.classList.remove('is-visible'), 3000);
    return;
  }
  showToast('按住话筒随口说一句', {
    actionLabel: '打字记一条',
    onAction: () => actions.openTextCapture?.({ personId: store.get().activePersonId || null }),
    ms: 3400,
  });
}

function setCancelVisual(on) {
  if (!hold || hold.cancelArmed === on) return;
  hold.cancelArmed = on;
  capsuleEl?.classList.toggle('is-cancelling', on);
  if (cancelHintEl) cancelHintEl.textContent = on ? '松开即可取消' : '上滑取消';
  if (on) haptic('voiceCancelCliff');
}

function beginHold(e, triggerEl) {
  if (hold) return;
  hapticPrepare('voiceArm');

  const p = activePerson();
  const current = {
    pointerId: e.pointerId ?? 'key',
    sessionId: 0,
    startY: e.clientY ?? 0,
    t0: performance.now(),
    revealed: false,
    cancelArmed: false,
    preRollTimer: 0,
    revealTimer: 0,
    tickTimer: 0,
    partialText: '',
    finished: false,
    triggerEl,
  };
  hold = current;
  triggerEl?.classList.add('is-holding');

  // 05 §8.3: Honor 40ms PRE_ROLL_MS so rapid accidental taps NEVER touch hardware mic / getUserMedia.
  current.preRollTimer = setTimeout(() => {
    current.preRollTimer = 0;
    if (hold !== current || current.finished) return;
    const sessionId = voiceFsm.acquire('journal_orb', {
      mode: 'capture',
      saveAudio: true,
      onPartial: (txt) => {
        if (hold !== current) return;
        current.partialText = txt;
        if (captionEl && txt.trim()) captionEl.textContent = txt.trim();
      },
      onRms: (rms) => {
        if (hold === current && current.revealed && orbEl) driveOrbRms(orbEl, rms);
      },
      onSilentWarning: () => {
        if (hold === current && captionEl && !current.partialText) {
          captionEl.textContent = '好像没听到声音，靠近一点说试试';
        }
      },
      onPermissionDenied: () => {
        if (hold !== current || current.finished) return;
        const intendedHold = current.revealed || performance.now() - current.t0 >= CAPSULE_REVEAL_MS;
        abortHold(true);
        whisper('麦克风或语音识别未开启，先打字记一句吧', 3200);
        if (intendedHold) actions.openTextCapture?.({ personId: p?.id || null });
      },
      onError: (ev) => {
        if (hold !== current || current.finished) return;
        const intendedHold = current.revealed || performance.now() - current.t0 >= CAPSULE_REVEAL_MS;
        abortHold(true);
        if (!intendedHold) {
          showHoldHint();
          return;
        }
        whisper(ev?.kind === 'denied' ? '麦克风没打开，先打字记一句吧' : '暂时没能开始录音，先打字记一句吧', 3000);
        actions.openTextCapture?.({ personId: p?.id || null });
      },
      onAutoCap: () => {
        if (hold === current) finishHold();
      },
    });
    if (!sessionId) {
      cleanupHold(current);
      whisper('语音通道正忙，请稍候一秒', 1800);
      return;
    }
    current.sessionId = sessionId;
  }, PRE_ROLL_MS);

  // Reveal inline porcelain capsule ONLY after 160ms hold guard (zero short-tap flash!).
  current.revealTimer = setTimeout(() => {
    if (hold !== current || current.finished) return;
    current.revealed = true;
    // Acoustic-safe <= 15ms voiceArm transient (zero continuous buzz during active mic!).
    haptic('voiceArm');
    ensureInlineCapsule();
    if (captionEl) captionEl.textContent = p ? `正在听 · 收进「${p.nameCn}」` : '正在听 · 自动归档';
    if (timerEl) timerEl.textContent = '0:00';
    if (cancelHintEl) cancelHintEl.textContent = '上滑取消';
    showCapsule();
    current.tickTimer = setInterval(() => {
      if (hold !== current) return;
      const elapsed = performance.now() - current.t0;
      if (timerEl) timerEl.textContent = formatTimer(elapsed);
      capsuleEl?.classList.toggle('is-near-cap', elapsed >= NEAR_CAP_MS);
    }, 200);
  }, CAPSULE_REVEAL_MS);
}

function moveHold(e) {
  if (!hold || hold.finished) return;
  if (e.pointerId !== undefined && e.pointerId !== hold.pointerId) return;
  const dy = (e.clientY ?? hold.startY) - hold.startY;
  setCancelVisual(isSlideUpCancel(dy));
}

function cleanupHold(current) {
  clearTimeout(current.preRollTimer);
  clearTimeout(current.revealTimer);
  clearInterval(current.tickTimer);
  current.triggerEl?.classList.remove('is-holding');
  if (hold === current) hold = null;
  hideCapsule();
}

function abortHold(silent = false) {
  if (!hold || hold.finished) return;
  const current = hold;
  current.finished = true;
  const sid = current.sessionId;
  cleanupHold(current);
  if (sid) voiceFsm.cancel(sid, { reason: 'user_cancel' });
  if (!silent) whisper('已取消，这段话没有留下', 1800);
}

async function finishHold() {
  if (!hold || hold.finished) return;
  const current = hold;
  current.finished = true;
  const durationMs = performance.now() - current.t0;
  const zone = current.cancelArmed ? 'cancel' : 'current';
  const sid = current.sessionId;
  const partialFallback = current.partialText;
  cleanupHold(current);

  const decision = decideRelease({ zone, durationMs });
  if (decision === 'cancel') {
    if (sid) voiceFsm.cancel(sid, { reason: 'slide_up_cancel' });
    whisper('已取消，这段话没有留下', 1800);
    return;
  }
  if (decision === 'too-short') {
    if (sid) voiceFsm.cancel(sid, { reason: 'short_tap' });
    haptic('light');
    showHoldHint();
    return;
  }

  haptic('voiceRelease');
  const res = sid ? await voiceFsm.stop(sid) : null;
  if (!res) return;

  const p = activePerson();
  const route = p ? { routeMode: 'current', activePersonId: p.id } : { routeMode: 'global', activePersonId: null };
  const transcript = String(res.transcript || partialFallback || '').trim();

  if (res.native) {
    if (res.blob || res.audioUrl) {
      submitVoice({
        blob: res.blob || null,
        audioUrl: res.audioUrl || null,
        draftId: res.draftId || null,
        clientTranscript: transcript,
        speechEngine: 'apple_native',
        audioDurationMs: res.durationMs,
        route,
      });
    } else if (transcript) {
      submitText({ text: transcript, route });
    } else {
      whisper('没有听清这句话，再按住说一次吧', 2200);
    }
    return;
  }

  if (!res.blob || res.blob.size < 200) {
    if (transcript) submitText({ text: transcript, route });
    else showHoldHint();
    return;
  }
  submitVoice({ blob: res.blob, audioDurationMs: res.durationMs, route });
}

function bindTrigger(btn) {
  if (!btn || btn.dataset.voiceBound) return;
  btn.dataset.voiceBound = '1';
  btn.addEventListener('contextmenu', (e) => e.preventDefault());
  btn.addEventListener('pointerdown', (e) => {
    if (e.button !== undefined && e.button !== 0) return;
    e.preventDefault();
    try { btn.setPointerCapture(e.pointerId); } catch { /* ignore */ }
    beginHold(e, btn);
  });
  btn.addEventListener('pointermove', moveHold);
  btn.addEventListener('pointerup', (e) => {
    if (hold && hold.pointerId === e.pointerId) finishHold();
  });
  btn.addEventListener('pointercancel', (e) => {
    if (hold && hold.pointerId === e.pointerId) abortHold();
  });
  btn.addEventListener('keydown', (e) => {
    if ((e.key === ' ' || e.key === 'Enter') && !e.repeat && !hold) {
      e.preventDefault();
      beginHold({}, btn);
    }
  });
  btn.addEventListener('keyup', (e) => {
    if ((e.key === ' ' || e.key === 'Enter') && hold && hold.pointerId === 'key') finishHold();
  });
}

export function initVoice() {
  // Bind the B1 44x44 Voice Well inside #capsule (and observe capsule re-renders if needed).
  const bindCurrent = () => {
    const well = $('#capsuleVoiceBtn') || $('#capsule .capsule-voice-btn') || $('#voiceOrb');
    if (well) bindTrigger(well);
  };
  bindCurrent();
  const capsule = $('#capsule');
  if (capsule && typeof MutationObserver === 'function') {
    new MutationObserver(bindCurrent).observe(capsule, { childList: true });
  }
  const typeBtn = $('#holdHintType');
  if (typeBtn) {
    typeBtn.addEventListener('click', () => {
      $('#holdHint')?.classList.remove('is-visible');
      actions.openTextCapture?.({ personId: store.get().activePersonId || null });
    });
  }
  globalThis.addEventListener?.('blur', () => { if (hold) abortHold(true); });
  if (isNativeSpeechAvailable?.()) ensureInlineCapsule();
}
