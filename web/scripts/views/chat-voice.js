// chat-voice.js — B4 Dual-Voice Controller for Cupid Chat + iOS left-edge swipe-to-pop.
// Delegates 100% of hardware mic/speech ownership to voice-fsm.js (zero double-recorder race!):
//   1. Inline hairline Dictation Mic: appends live Chinese to textarea, NEVER auto-sends on silence.
//   2. Detached 52px Living Orb: Tap (< 240ms) = Hands-Free Live Voice; Hold (>= 240ms) = Walkie-Talkie.
// Zero --bar-scale, zero sparkle glyphs, scoped AbortController teardown.
import { h, svg } from '../dom.js';
import { api } from '../api/endpoints.js';
import { friendlyMessage } from '../api/client.js';
import { MIC_SVG } from '../icons.js';
import { haptic, hapticPrepare } from '../native-bridge.js';
import { pickMimeType, extensionFor, CANCEL_DY } from '../voice-logic.js';
import { voiceFsm } from '../voice-fsm.js';
import { setOrbState, driveOrbRms } from '../lib/orb.js';
import { whisper } from './toast.js';

const TAP_MAX_MS = 240;
const EDGE_POP_PX = 24;
const INTERACTIVE_SEL = 'button, input, textarea, a, .chat-attached-tray';

export function clearSwipeStyles(root) {
  if (!root) return;
  root.classList.remove('is-swiping');
  root.style.transform = '';
  root.style.opacity = '';
  root.style.transition = '';
}

/** Left-edge iOS swipe-right-to-pop (x < 24px) with scoped AbortSignal teardown. */
export function bindChatEdgePop({ root, isDrawerOpen, isVoiceBusy, onSwipePop, signal }) {
  let swipe = null;
  let suppressClickUntil = 0;
  const opts = signal ? { signal } : undefined;

  root.addEventListener('click', (e) => {
    if (performance.now() < suppressClickUntil) {
      e.preventDefault();
      e.stopPropagation();
    }
  }, { capture: true, signal });

  root.addEventListener('pointerdown', (e) => {
    if ((e.button !== undefined && e.button !== 0) || !root.classList.contains('is-active')) return;
    if (isDrawerOpen?.() || isVoiceBusy?.() || e.target?.closest?.(INTERACTIVE_SEL)) return;
    const rect = root.getBoundingClientRect();
    if (e.clientX - rect.left >= EDGE_POP_PX) return;
    swipe = { id: e.pointerId, x: e.clientX, y: e.clientY, prevX: e.clientX, prevT: performance.now(), t0: performance.now(), width: rect.width || 390, locked: false, canceled: false, dx: 0, vx: 0 };
  }, opts);

  root.addEventListener('pointermove', (e) => {
    if (!swipe || swipe.id !== e.pointerId || swipe.canceled) return;
    const dx = e.clientX - swipe.x;
    const dy = e.clientY - swipe.y;
    if (!swipe.locked) {
      if (Math.hypot(dx, dy) < 10) return;
      if (dx <= 0 || Math.abs(dy) > Math.abs(dx) * 1.15) return (swipe.canceled = true);
      swipe.locked = true;
      root.classList.add('is-swiping');
      root.style.transition = 'none';
      try { root.setPointerCapture?.(e.pointerId); } catch { /* ignore */ }
    }
    const now = performance.now();
    swipe.vx = 0.45 * ((e.clientX - swipe.prevX) / Math.max(1, now - swipe.prevT)) + 0.55 * swipe.vx;
    swipe.prevX = e.clientX;
    swipe.prevT = now;
    swipe.dx = Math.max(0, dx);
    root.style.transform = `translateX(${swipe.dx}px)`;
    root.style.opacity = String(Math.max(0.55, 1 - Math.min(1, swipe.dx / swipe.width) * 0.36));
  }, opts);

  const end = (e, canceled) => {
    if (!swipe || swipe.id !== e.pointerId) return;
    const cur = swipe;
    swipe = null;
    if (!cur.locked) return;
    suppressClickUntil = performance.now() + 260;
    try { root.releasePointerCapture?.(e.pointerId); } catch { /* ignore */ }
    root.classList.remove('is-swiping');
    const v = Math.max(cur.vx, cur.dx / Math.max(1, performance.now() - cur.t0));
    if (!canceled && (cur.dx > 96 || v > 0.5)) {
      haptic('light');
      root.style.transition = 'transform 240ms cubic-bezier(0.22,1,0.36,1), opacity 220ms ease';
      root.style.transform = 'translateX(100%)';
      root.style.opacity = '0';
      onSwipePop();
    } else {
      root.style.transition = 'transform 280ms cubic-bezier(0.22,1,0.36,1), opacity 220ms ease';
      root.style.transform = 'translateX(0px)';
      root.style.opacity = '1';
      setTimeout(() => !swipe && root.classList.contains('is-active') && clearSwipeStyles(root), 300);
    }
  };
  root.addEventListener('pointerup', (e) => end(e, false), opts);
  root.addEventListener('pointercancel', (e) => end(e, true), opts);
}

/** B4 Dual-Voice controller for Cupid Chat delegating 100% of audio hardware to voiceFsm. */
export function createChatVoice({ pillBar, orbBtn, orbEl, inputEl, getPersonId, canRecord, onDirectSend, onDictateAppend, onStateChange }) {
  const abort = new AbortController();
  const { signal } = abort;
  let active = null; // { mode: 'dictate'|'direct_live'|'direct_hold', sessionId, liveText, baseText, canceling, startY }
  let transcribing = false;
  let jobSeq = 0;

  const inlineMicBtn = h(
    'button',
    { type: 'button', id: 'inlineMicBtn', class: 'chat-inline-mic-btn', 'aria-label': '框内语音转文字（不自动发送）' },
    svg(MIC_SVG, 'chat-inline-mic-icon'),
  );
  const dictateDoneBtn = h('button', { type: 'button', id: 'dictateDoneBtn', class: 'chat-dictate-done-btn', text: '完成', 'aria-label': '完成转文字', hidden: true });
  const statusChip = h('span', { class: 'chat-voice-live-chip', role: 'status', 'aria-live': 'polite', hidden: true });

  function syncUi() {
    const mode = active?.mode || 'idle';
    const isDictate = mode === 'dictate';
    const isDirect = mode === 'direct_live' || mode === 'direct_hold';
    inlineMicBtn.classList.toggle('is-active', isDictate);
    dictateDoneBtn.hidden = !isDictate;
    pillBar.classList.toggle('is-dictating', isDictate);
    pillBar.classList.toggle('is-direct-voice', isDirect);
    pillBar.classList.toggle('is-canceling', Boolean(active?.canceling));
    const showChip = isDirect || transcribing;
    statusChip.hidden = !showChip;
    statusChip.classList.toggle('is-open', showChip);
    statusChip.classList.toggle('is-canceling', Boolean(active?.canceling));
    if (transcribing) statusChip.textContent = '正在听清刚才那句…';
    else if (active?.canceling) statusChip.textContent = '松手取消';
    else if (mode === 'direct_live') statusChip.textContent = active?.liveText || '正在听 · 轻点灵珠发送';
    else if (mode === 'direct_hold') statusChip.textContent = active?.liveText || '松开发送 · 上滑取消';
    if (orbEl) setOrbState(orbEl, isDirect ? 'listening' : transcribing ? 'thinking' : 'idle');
    onStateChange?.();
  }

  function startSession(mode, extra = {}) {
    if (active || transcribing || !canRecord?.()) return false;
    const source = mode === 'dictate' ? 'chat_dictate' : 'chat_direct_orb';
    const cur = { mode, sessionId: 0, liveText: '', baseText: inputEl?.value || '', canceling: false, startY: extra.startY ?? 0 };
    const sessionId = voiceFsm.acquire(source, {
      mode: mode === 'dictate' ? 'dictation' : 'capture',
      saveAudio: false,
      onPartial: (text) => {
        if (active !== cur) return;
        cur.liveText = String(text || '');
        if (cur.mode === 'dictate') onDictateAppend?.(cur.liveText, cur.baseText, { preview: true });
        else syncUi();
      },
      onRms: (rms) => active === cur && orbEl && driveOrbRms(orbEl, rms),
      onSilentWarning: () => active === cur && whisper('好像没听到声音，靠近话筒再说一句', 2000),
      onPermissionDenied: () => {
        if (active === cur) { active = null; syncUi(); }
        whisper('麦克风或语音识别未打开，直接打字问丘比特吧', 2600);
      },
      onError: (err) => {
        if (active === cur) { active = null; syncUi(); }
        whisper(err?.kind === 'denied' ? '麦克风没打开，直接打字问丘比特吧' : '这台设备暂时录不了音，打字也一样', 2600);
      },
      onInterrupted: () => active === cur && finishSession(false),
    });
    if (!sessionId) return false;
    cur.sessionId = sessionId;
    active = cur;
    haptic(mode === 'dictate' ? 'soft' : 'voiceArm', { intensity: mode === 'dictate' ? 0.5 : 0.58 });
    syncUi();
    return true;
  }

  async function finishSession(forceCancel = false) {
    const cur = active;
    if (!cur) return;
    active = null;
    if (forceCancel || cur.canceling) {
      voiceFsm.cancel(cur.sessionId);
      syncUi();
      return;
    }
    haptic(cur.mode === 'dictate' ? 'light' : 'voiceRelease', { intensity: 0.45 });
    const res = await voiceFsm.stop(cur.sessionId);
    if (!res) return syncUi();
    if (res.native) {
      const text = String(res.transcript || cur.liveText || '').trim();
      syncUi();
      if (!text) return;
      if (cur.mode === 'dictate') onDictateAppend?.(text, cur.baseText);
      else onDirectSend?.(text);
      return;
    }
    const { blob, mime } = res;
    if (!blob || blob.size < 100) {
      syncUi();
      return whisper('好像没听到声音，再说一次吧', 2000);
    }
    const seq = ++jobSeq;
    transcribing = true;
    syncUi();
    const picked = mime || blob.type || pickMimeType((t) => globalThis.MediaRecorder?.isTypeSupported?.(t));
    try {
      const out = await api.transcribeVoice({ blob, filename: `cupid-voice.${extensionFor(picked)}`, personId: getPersonId() });
      if (seq !== jobSeq) return;
      transcribing = false;
      syncUi();
      const text = String(out?.transcript || '').trim();
      if (!text) return whisper('好像没听清，再说一次吧', 2200);
      if (cur.mode === 'dictate') onDictateAppend?.(text, cur.baseText);
      else onDirectSend?.(text);
    } catch (err) {
      if (seq !== jobSeq) return;
      transcribing = false;
      syncUi();
      haptic('warning');
      whisper(err?.message || friendlyMessage(err?.code, err?.status) || '语音转文字没连上，先打字吧', 2600);
    }
  }

  inlineMicBtn.addEventListener('pointerdown', () => hapticPrepare('soft'), { signal });
  inlineMicBtn.addEventListener('click', () => {
    if (active?.mode === 'dictate') finishSession(false);
    else if (!active) startSession('dictate');
  }, { signal });
  dictateDoneBtn.addEventListener('pointerdown', () => hapticPrepare('light'), { signal });
  dictateDoneBtn.addEventListener('click', () => active?.mode === 'dictate' && finishSession(false), { signal });

  let orbDown = null;
  const cancelDy = typeof CANCEL_DY === 'number' ? CANCEL_DY : -72;
  orbBtn.addEventListener('pointerdown', (e) => {
    if ((e.button !== undefined && e.button !== 0) || orbBtn.dataset.mode !== 'orb') return;
    if (active?.mode === 'direct_live') return finishSession(false);
    if (active || transcribing || !canRecord?.()) return;
    hapticPrepare('voiceArm');
    orbDown = { id: e.pointerId, startY: e.clientY, t0: performance.now(), holdTimer: null };
    try { orbBtn.setPointerCapture?.(e.pointerId); } catch { /* ignore */ }
    orbDown.holdTimer = setTimeout(() => orbDown && startSession('direct_hold', { startY: orbDown.startY }), TAP_MAX_MS);
  }, { signal });
  orbBtn.addEventListener('pointermove', (e) => {
    if (!active || active.mode !== 'direct_hold' || !orbDown || orbDown.id !== e.pointerId) return;
    const wantCancel = e.clientY - active.startY < cancelDy;
    if (wantCancel !== active.canceling) {
      active.canceling = wantCancel;
      haptic(wantCancel ? 'voiceCancelCliff' : 'light');
      syncUi();
    }
  }, { signal });
  const endOrb = (e, canceled) => {
    if (!orbDown || orbDown.id !== e.pointerId) return;
    const down = orbDown;
    orbDown = null;
    clearTimeout(down.holdTimer);
    if (active?.mode === 'direct_hold') finishSession(canceled);
    else if (!canceled && !active && performance.now() - down.t0 < TAP_MAX_MS) startSession('direct_live');
  };
  orbBtn.addEventListener('pointerup', (e) => endOrb(e, false), { signal });
  orbBtn.addEventListener('pointercancel', (e) => endOrb(e, true), { signal });

  function cancel() {
    jobSeq += 1;
    transcribing = false;
    if (orbDown) { clearTimeout(orbDown.holdTimer); orbDown = null; }
    finishSession(true);
  }
  return {
    inlineMicBtn,
    dictateDoneBtn,
    statusChip,
    cancel,
    destroy: () => (cancel(), abort.abort()),
    isBusy: () => Boolean(active || transcribing),
    isTranscribing: () => transcribing,
  };
}
