// chat-voice.js — voice-first hold-to-speak mic inside Cupid's composer + iOS 18 edge swipe gestures.
// Hold >= 350ms → MediaRecorder + live AnalyserNode bars → /v1/voice/transcribe → auto-send.
// Drag up > 60px → crimson cancel; short tap (< 350ms) → gentle whisper, zero network call.
import { h, svg } from '../dom.js';
import { api } from '../api/endpoints.js';
import { friendlyMessage } from '../api/client.js';
import { MIC_SVG } from '../icons.js';
import { haptic, setAudioSession, prefersReducedMotion } from '../native-bridge.js';
import { pickMimeType, extensionFor, formatTimer, MAX_HOLD_MS } from '../voice-logic.js';
import { createRecorder, MicError } from '../recorder.js';
import { whisper } from './toast.js';

const CANCEL_DRAG_PX = 60;
const MIN_SEND_MS = 350;
const SWIPE_EDGE_PX = 28;
const INTERACTIVE_SEL = 'button, input, textarea, a, .chat-attached-tray';
const BAR_WEIGHTS = [0.46, 0.72, 0.94, 0.78, 1.0, 0.82, 0.92, 0.68, 0.44];

export function clearSwipeStyles(root) {
  root.classList.remove('is-swiping');
  root.style.transform = '';
  root.style.opacity = '';
  root.style.transition = '';
}

function clearDrawerSwipe(drawer) {
  drawer.classList.remove('is-swiping');
  drawer.style.transform = '';
  drawer.style.transition = '';
}

/** Apple iOS 18 left-edge swipe-right-to-pop on #chat + finger-tracked right-edge drawer swipe. */
export function bindChatGestures({ root, edge, drawer, isVoiceBusy, onOpenDrawer, onCloseDrawer, onSwipePop }) {
  let suppressClickUntil = 0;
  const guardClick = (e) => {
    if (performance.now() < suppressClickUntil) {
      e.preventDefault();
      e.stopPropagation();
    }
  };
  root.addEventListener('click', guardClick, true);

  let rStart = null;
  edge.addEventListener('pointerdown', (e) => {
    if (e.button !== undefined && e.button !== 0) return;
    rStart = { x: e.clientX, id: e.pointerId };
    try { edge.setPointerCapture?.(e.pointerId); } catch { /* ignore */ }
  });
  edge.addEventListener('pointermove', (e) => {
    if (rStart && rStart.id === e.pointerId && rStart.x - e.clientX > 28) {
      rStart = null;
      haptic('light');
      onOpenDrawer();
    }
  });
  edge.addEventListener('pointerup', () => (rStart = null));
  edge.addEventListener('pointercancel', () => (rStart = null));

  let dSwipe = null;
  drawer.addEventListener('pointerdown', (e) => {
    if (e.button !== undefined && e.button !== 0) return;
    if (!drawer.classList.contains('is-open')) return;
    dSwipe = { id: e.pointerId, x: e.clientX, y: e.clientY, prevX: e.clientX, prevT: performance.now(), t0: performance.now(), locked: false, canceled: false, dx: 0, vx: 0, hasSample: false };
  });
  drawer.addEventListener('pointermove', (e) => {
    if (!dSwipe || dSwipe.id !== e.pointerId || dSwipe.canceled) return;
    const dx = e.clientX - dSwipe.x;
    const dy = e.clientY - dSwipe.y;
    if (!dSwipe.locked) {
      if (Math.hypot(dx, dy) < 10) return;
      if (dx <= 0 || Math.abs(dy) > Math.abs(dx) * 1.15) return (dSwipe.canceled = true);
      dSwipe.locked = true;
      drawer.classList.add('is-swiping');
      drawer.style.transition = 'none';
      try { drawer.setPointerCapture?.(e.pointerId); } catch { /* ignore */ }
    }
    const now = performance.now();
    const inst = (e.clientX - dSwipe.prevX) / Math.max(1, now - dSwipe.prevT);
    dSwipe.vx = dSwipe.hasSample ? 0.45 * inst + 0.55 * dSwipe.vx : inst;
    dSwipe.hasSample = true;
    dSwipe.prevX = e.clientX;
    dSwipe.prevT = now;
    dSwipe.dx = Math.max(0, dx);
    drawer.style.transform = `translateX(${dSwipe.dx}px)`;
  });
  drawer.addEventListener('touchmove', (e) => dSwipe?.locked && e.preventDefault(), { passive: false });
  const endDrawerSwipe = (e, canceled) => {
    if (!dSwipe || dSwipe.id !== e.pointerId) return;
    const cur = dSwipe;
    dSwipe = null;
    if (!cur.locked) return;
    suppressClickUntil = performance.now() + 260;
    try { drawer.releasePointerCapture?.(e.pointerId); } catch { /* ignore */ }
    drawer.classList.remove('is-swiping');
    const velocity = Math.max(cur.vx, cur.dx / Math.max(1, performance.now() - cur.t0));
    if (!canceled && (cur.dx > 64 || velocity > 0.45)) {
      haptic('light');
      clearDrawerSwipe(drawer);
      onCloseDrawer();
    } else {
      drawer.style.transition = 'transform 0.28s var(--ease-spring)';
      drawer.style.transform = 'translateX(0px)';
      setTimeout(() => !dSwipe && clearDrawerSwipe(drawer), 290);
    }
  };
  drawer.addEventListener('pointerup', (e) => endDrawerSwipe(e, false));
  drawer.addEventListener('pointercancel', (e) => endDrawerSwipe(e, true));

  let swipe = null;
  root.addEventListener('pointerdown', (e) => {
    if (e.button !== undefined && e.button !== 0) return;
    if (!root.classList.contains('is-active') || drawer.classList.contains('is-open') || isVoiceBusy?.()) return;
    if (e.target?.closest?.(INTERACTIVE_SEL)) return;
    const rect = root.getBoundingClientRect();
    if (e.clientX - rect.left > SWIPE_EDGE_PX) return;
    swipe = { id: e.pointerId, x: e.clientX, y: e.clientY, prevX: e.clientX, prevT: performance.now(), t0: performance.now(), width: rect.width || 390, locked: false, canceled: false, dx: 0, vx: 0, hasSample: false };
  });
  root.addEventListener('pointermove', (e) => {
    if (!swipe || swipe.id !== e.pointerId || swipe.canceled) return;
    const dx = e.clientX - swipe.x;
    const dy = e.clientY - swipe.y;
    if (!swipe.locked) {
      if (Math.hypot(dx, dy) < 8) return;
      if (dx <= 0 || Math.abs(dy) > Math.abs(dx) * 1.15) return (swipe.canceled = true);
      swipe.locked = true;
      root.classList.add('is-swiping');
      root.style.transition = 'none';
      try { root.setPointerCapture?.(e.pointerId); } catch { /* ignore */ }
    }
    const now = performance.now();
    const inst = (e.clientX - swipe.prevX) / Math.max(1, now - swipe.prevT);
    swipe.vx = swipe.hasSample ? 0.45 * inst + 0.55 * swipe.vx : inst;
    swipe.hasSample = true;
    swipe.prevX = e.clientX;
    swipe.prevT = now;
    swipe.dx = Math.max(0, dx);
    root.style.transform = `translateX(${swipe.dx}px)`;
    root.style.opacity = String(Math.max(0.55, 1 - Math.min(1, swipe.dx / swipe.width) * 0.36));
  });
  root.addEventListener('touchmove', (e) => swipe?.locked && e.preventDefault(), { passive: false });
  const endSwipe = (e, canceled) => {
    if (!swipe || swipe.id !== e.pointerId) return;
    const cur = swipe;
    swipe = null;
    if (!cur.locked) return;
    suppressClickUntil = performance.now() + 260;
    try { root.releasePointerCapture?.(e.pointerId); } catch { /* ignore */ }
    root.classList.remove('is-swiping');
    const velocity = Math.max(cur.vx, cur.dx / Math.max(1, performance.now() - cur.t0));
    if (!canceled && (cur.dx > 100 || velocity > 0.5)) {
      haptic('light');
      root.style.transition = 'transform 0.24s var(--ease-silk), opacity 0.22s ease';
      root.style.transform = 'translateX(100%)';
      root.style.opacity = '0';
      onSwipePop();
    } else {
      root.style.transition = 'transform 0.3s var(--ease-spring), opacity 0.24s ease';
      root.style.transform = 'translateX(0px)';
      root.style.opacity = '1';
      setTimeout(() => !swipe && root.classList.contains('is-active') && clearSwipeStyles(root), 310);
    }
  };
  root.addEventListener('pointerup', (e) => endSwipe(e, false));
  root.addEventListener('pointercancel', (e) => endSwipe(e, true));
}

export function createChatVoice({ pillBar, getPersonId, canRecord, onTranscript, onStateChange }) {
  let hold = null;
  let transcribing = false;
  let transcribeSeq = 0;
  let sessionActive = false;

  const enterSession = () => {
    if (sessionActive) return;
    sessionActive = true;
    setAudioSession?.('playAndRecord');
  };
  const leaveSession = () => {
    if (!sessionActive) return;
    sessionActive = false;
    setAudioSession?.('deactivate');
  };

  const micBtn = h(
    'button',
    { type: 'button', class: 'chat-mic-btn', 'aria-label': '按住说话，松开直接问丘比特' },
    h('span', { class: 'chat-mic-halo', 'aria-hidden': 'true' }),
    svg(MIC_SVG, 'chat-mic-icon'),
  );
  const bars = BAR_WEIGHTS.map((_, i) => h('span', { class: 'cv-bar', style: { '--bar-i': String(i) } }));
  const barsWrap = h('span', { class: 'chat-voice-bars', 'aria-hidden': 'true' }, ...bars);
  const sparkEl = h('span', { class: 'chat-voice-spark', 'aria-hidden': 'true', text: '✦', hidden: true });
  const statusEl = h('span', { class: 'chat-voice-status', text: '正在听…' });
  const timerEl = h('span', { class: 'chat-voice-timer', text: '0:00' });
  const hintEl = h('span', { class: 'chat-voice-hint', text: '松开发送 · 上滑取消' });
  const overlayEl = h(
    'div',
    { class: 'chat-voice-overlay', role: 'status', 'aria-live': 'polite', hidden: true },
    barsWrap,
    sparkEl,
    h('span', { class: 'chat-voice-copy' }, statusEl, timerEl, hintEl),
  );

  function setOverlayMode(mode) {
    const isRec = mode === 'recording';
    const isCancel = mode === 'cancel';
    const isTrans = mode === 'transcribing';
    overlayEl.hidden = !(isRec || isCancel || isTrans);
    overlayEl.classList.toggle('is-canceling', isCancel);
    overlayEl.classList.toggle('is-transcribing', isTrans);
    pillBar.classList.toggle('is-recording', isRec || isCancel);
    pillBar.classList.toggle('is-canceling', isCancel);
    pillBar.classList.toggle('is-transcribing', isTrans);
    micBtn.classList.toggle('is-holding', isRec || isCancel);
    micBtn.classList.toggle('is-canceling', isCancel);
    barsWrap.hidden = isTrans;
    sparkEl.hidden = !isTrans;
    timerEl.hidden = !isRec;
    hintEl.hidden = !isRec;
    if (isCancel) statusEl.textContent = '松手取消';
    else if (isTrans) statusEl.textContent = '正在听清刚才那句…';
    else if (isRec) statusEl.textContent = '正在听…';
    onStateChange?.();
  }

  function applyStaticBars(canceling, level = 0.25) {
    const gain = canceling ? 0.28 : 0.42 + level * 0.6;
    for (let i = 0; i < bars.length; i++) {
      bars[i].style.setProperty('--bar-scale', Math.max(0.24, Math.min(0.92, BAR_WEIGHTS[i] * gain)).toFixed(3));
    }
  }

  function animateBars(now) {
    if (!hold) return;
    if (prefersReducedMotion()) return applyStaticBars(hold.canceling, hold.recReady ? hold.rec.level() : 0.2);
    const raw = hold.recReady ? hold.rec.level() : 0.14;
    hold.level = hold.level * 0.68 + raw * 0.32;
    const t = now * 0.009;
    const gain = hold.canceling ? 0.25 : 0.32 + hold.level * 1.45;
    for (let i = 0; i < bars.length; i++) {
      const wave = 0.55 + 0.45 * Math.sin(t + i * 0.85);
      bars[i].style.setProperty('--bar-scale', Math.max(0.22, Math.min(1, BAR_WEIGHTS[i] * gain * wave)).toFixed(3));
    }
    hold.raf = requestAnimationFrame(animateBars);
  }

  function resetIdle() {
    setOverlayMode('idle');
    for (const b of bars) b.style.removeProperty('--bar-scale');
  }

  async function beginHold(e) {
    if (hold || transcribing || !canRecord?.()) return;
    const personId = getPersonId();
    if (!personId) return;
    haptic('medium');
    enterSession();
    const reduced = prefersReducedMotion();
    const cur = {
      pointerId: e.pointerId ?? 'key',
      personId,
      startY: typeof e.clientY === 'number' ? e.clientY : 0,
      t0: performance.now(),
      canceling: false,
      finished: false,
      rec: createRecorder(),
      recReady: false,
      raf: 0,
      timer: 0,
      level: 0.14,
    };
    hold = cur;
    timerEl.textContent = '0:00';
    setOverlayMode('recording');
    if (reduced) applyStaticBars(false, 0.24);
    else cur.raf = requestAnimationFrame(animateBars);
    cur.timer = setInterval(() => {
      if (hold !== cur) return;
      const elapsed = performance.now() - cur.t0;
      timerEl.textContent = formatTimer(elapsed);
      if (prefersReducedMotion()) applyStaticBars(cur.canceling, cur.recReady ? cur.rec.level() : 0.2);
      if (elapsed >= MAX_HOLD_MS) finishHold(false);
    }, 200);
    try {
      await cur.rec.start();
      cur.recReady = true;
      if (cur.finished) cur.rec.discard();
    } catch (err) {
      if (hold === cur) {
        cancelAnimationFrame(cur.raf);
        clearInterval(cur.timer);
        hold = null;
        resetIdle();
      }
      cur.rec.discard();
      leaveSession();
      haptic('warning');
      const kind = err instanceof MicError ? err.kind : 'unsupported';
      whisper(kind === 'denied' ? '麦克风没打开，直接打字问丘比特吧' : '这台设备暂时录不了音，直接打字问丘比特吧', 2800);
    }
  }

  async function finishHold(forceCancel = false) {
    if (!hold || hold.finished) return;
    const cur = hold;
    cur.finished = true;
    hold = null;
    cancelAnimationFrame(cur.raf);
    clearInterval(cur.timer);
    const durationMs = performance.now() - cur.t0;

    if (forceCancel || cur.canceling) {
      cur.rec.discard();
      leaveSession();
      return resetIdle();
    }
    if (durationMs < MIN_SEND_MS) {
      cur.rec.discard();
      leaveSession();
      resetIdle();
      haptic('light');
      return whisper('按住说话，松开直接问丘比特', 2200);
    }
    if (!cur.recReady) {
      cur.rec.discard();
      leaveSession();
      resetIdle();
      return whisper('麦克风刚准备好，再按住说一次吧', 2400);
    }
    let stopped;
    try {
      stopped = await cur.rec.stop();
    } catch {
      leaveSession();
      resetIdle();
      return whisper('刚刚没录上，再按住说一次吧', 2400);
    }
    leaveSession();
    const { blob, mime } = stopped || {};
    if (!blob || blob.size < 100) {
      resetIdle();
      return whisper('好像没听到声音，再按住说一次吧', 2200);
    }

    const jobId = ++transcribeSeq;
    transcribing = true;
    setOverlayMode('transcribing');
    const picked = mime || blob.type || pickMimeType((t) => globalThis.MediaRecorder?.isTypeSupported?.(t));
    try {
      const res = await api.transcribeVoice({ blob, filename: `cupid-voice.${extensionFor(picked)}`, personId: cur.personId });
      if (jobId !== transcribeSeq || getPersonId() !== cur.personId) return;
      transcribing = false;
      resetIdle();
      const transcript = String(res?.transcript || '').trim();
      if (!transcript) return whisper('好像没听清，再按住说一次吧', 2400);
      haptic('light');
      onTranscript?.(transcript);
    } catch (err) {
      if (jobId !== transcribeSeq) return;
      transcribing = false;
      resetIdle();
      haptic('warning');
      whisper(err?.message || friendlyMessage(err?.code, err?.status) || '语音转文字没连上，先打字问丘比特吧', 2800);
    }
  }

  function cancel() {
    transcribeSeq += 1;
    transcribing = false;
    if (hold) {
      const cur = hold;
      cur.finished = true;
      hold = null;
      cancelAnimationFrame(cur.raf);
      clearInterval(cur.timer);
      cur.rec.discard();
    }
    leaveSession();
    resetIdle();
  }

  micBtn.addEventListener('contextmenu', (e) => e.preventDefault());
  micBtn.addEventListener('pointerdown', (e) => {
    if (e.button !== undefined && e.button !== 0) return;
    e.preventDefault();
    try { micBtn.setPointerCapture(e.pointerId); } catch { /* ignore */ }
    beginHold(e);
  });
  micBtn.addEventListener('pointermove', (e) => {
    if (!hold || (e.pointerId !== undefined && e.pointerId !== hold.pointerId)) return;
    const wantCancel = hold.startY - e.clientY > CANCEL_DRAG_PX;
    if (wantCancel === hold.canceling) return;
    hold.canceling = wantCancel;
    haptic(wantCancel ? 'warning' : 'selection');
    setOverlayMode(wantCancel ? 'cancel' : 'recording');
    if (prefersReducedMotion()) applyStaticBars(wantCancel, hold.recReady ? hold.rec.level() : 0.2);
  });
  micBtn.addEventListener('pointerup', (e) => hold && hold.pointerId === e.pointerId && finishHold(false));
  micBtn.addEventListener('pointercancel', (e) => hold && hold.pointerId === e.pointerId && finishHold(true));
  micBtn.addEventListener('keydown', (e) => {
    if ((e.key === ' ' || e.key === 'Enter') && !e.repeat && !hold) {
      e.preventDefault();
      beginHold({});
    }
  });
  micBtn.addEventListener('keyup', (e) => {
    if ((e.key === ' ' || e.key === 'Enter') && hold && hold.pointerId === 'key') {
      e.preventDefault();
      finishHold(false);
    }
  });
  micBtn.addEventListener('click', (e) => e.preventDefault());
  globalThis.addEventListener?.('blur', () => hold && finishHold(true));

  return { micBtn, overlayEl, cancel, isBusy: () => Boolean(hold || transcribing), isTranscribing: () => transcribing };
}
