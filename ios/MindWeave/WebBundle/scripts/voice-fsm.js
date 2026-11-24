// voice-fsm.js — Single-Active-Session VoiceSessionArbiter (03e §2.1 frozen contract).
// Enforces B4 dual-voice mutex, tweb#728 monotonic sessionId + ARMING + inFlightStop locks,
// mwNativeUI.setIdleTimer recording screen-wake guard, 2.5s -55 dBFS silent-mic watchdog,
// pre-hold mwSpeech.checkPermissions(), and audio.interrupted recovery.
import {
  isNativeSpeechAvailable,
  mwSpeech,
  mwNativeUI,
  onNativeEvent,
  haptic,
} from './native-bridge.js';
import { createRecorder, MicError } from './recorder.js';
import { SILENT_MIC_MS, SILENT_DBFS, rmsToDbfs, MAX_HOLD_MS } from './voice-logic.js';
import { actions } from './actions.js';

const VALID_SOURCES = new Set([
  'journal_orb',
  'chat_dictate',
  'chat_direct_orb',
  'onboarding',
  'playback',
]);

let state = 'IDLE'; // IDLE | ARMING | LISTENING | STOPPING | PLAYBACK
let activeSource = 'none';
let sessionSeq = 0;
let activeSession = null; // { id, source, mode, native, rec, opts, startedAt, silentTimer, capTimer, rmsRaf, heardAudio, stopped }
let inFlightStop = null;
let bridgeWired = false;
const listeners = new Set();

function emitState() {
  const snap = getState();
  for (const fn of listeners) {
    try { fn(snap); } catch { /* ignore */ }
  }
}

export function getState() {
  return { state, activeSource, sessionId: activeSession?.id || 0 };
}

export function subscribeVoiceFsm(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function setIdleWake(on) {
  try { mwNativeUI?.setIdleTimer?.(Boolean(on)); } catch { /* ignore */ }
}

function clearTimers(sess) {
  if (!sess) return;
  if (sess.silentTimer) { clearTimeout(sess.silentTimer); sess.silentTimer = 0; }
  if (sess.capTimer) { clearTimeout(sess.capTimer); sess.capTimer = 0; }
  if (sess.rmsRaf) { cancelAnimationFrame(sess.rmsRaf); sess.rmsRaf = 0; }
}

function wireNativeBridgeOnce() {
  if (bridgeWired) return;
  bridgeWired = true;
  if (typeof onNativeEvent === 'function') {
    onNativeEvent('speech.partial', (ev) => {
      const s = activeSession;
      if (!s || !s.native || s.stopped) return;
      if (ev?.sessionId && ev.sessionId !== s.nativeId) return;
      const text = String(ev?.text ?? ev?.transcript ?? '');
      if (text.trim()) s.heardAudio = true;
      s.opts?.onPartial?.(text, ev);
    });
    onNativeEvent('speech.rms', (ev) => {
      const s = activeSession;
      if (!s || !s.native || s.stopped) return;
      const rms = Math.max(0, Math.min(1, Number(ev?.rms ?? ev?.level ?? 0)));
      if (rmsToDbfs(rms) > SILENT_DBFS) s.heardAudio = true;
      s.opts?.onRms?.(rms);
    });
    onNativeEvent('speech.final', (ev) => {
      const s = activeSession;
      if (!s || !s.native) return;
      if (ev?.sessionId && ev.sessionId !== s.nativeId) return;
      s.finalPayload = ev;
      if (s.resolveFinal) {
        const res = s.resolveFinal;
        s.resolveFinal = null;
        res(ev);
      }
    });
    onNativeEvent('speech.error', (ev) => {
      const s = activeSession;
      if (!s || !s.native) return;
      s.opts?.onError?.(ev);
      if (s.resolveFinal) {
        const res = s.resolveFinal;
        s.resolveFinal = null;
        res({ error: ev?.code || 'speech_error', message: ev?.message });
      }
      if (activeSession === s) cancel(s.id, { reason: 'native_error' });
    });
    onNativeEvent('audio.interrupted', (ev) => {
      const s = activeSession;
      if (!s) return;
      if (s.source === 'playback') {
        actions.stopActiveAudio?.();
        releasePlayback();
        return;
      }
      // Preserve in-progress dictation/recording cleanly without losing textarea draft.
      s.opts?.onInterrupted?.(ev);
      stop(s.id, { interrupted: true });
    });
  }
  if (typeof window !== 'undefined') {
    window.addEventListener('pagehide', () => {
      if (activeSession) cancel(activeSession.id, { reason: 'pagehide' });
      setIdleWake(false);
    });
  }
}

function startSilentWatchdog(sess) {
  sess.silentTimer = setTimeout(() => {
    if (activeSession !== sess || sess.stopped || sess.heardAudio) return;
    sess.opts?.onSilentWarning?.();
  }, SILENT_MIC_MS);
  const maxMs = sess.opts?.maxMs || MAX_HOLD_MS;
  sess.capTimer = setTimeout(() => {
    if (activeSession !== sess || sess.stopped) return;
    sess.opts?.onAutoCap?.();
    stop(sess.id, { autoCap: true });
  }, maxMs);
}

function startWebRmsLoop(sess) {
  let lastTick = 0;
  const pump = (now) => {
    if (activeSession !== sess || sess.stopped || !sess.rec) return;
    if (now - lastTick >= 33) { // 30fps cap per 04d benchmark
      lastTick = now;
      const rms = sess.rec.level();
      if (rmsToDbfs(rms) > SILENT_DBFS) sess.heardAudio = true;
      sess.opts?.onRms?.(rms);
    }
    sess.rmsRaf = requestAnimationFrame(pump);
  };
  sess.rmsRaf = requestAnimationFrame(pump);
}

async function ensureNativePermission(opts) {
  if (!isNativeSpeechAvailable?.() || !mwSpeech?.checkPermissions) return { granted: true };
  try {
    const res = await mwSpeech.checkPermissions();
    if (res && res.granted === false) {
      opts?.onPermissionDenied?.(res);
      return { granted: false, status: res };
    }
    return { granted: true, status: res };
  } catch {
    return { granted: true };
  }
}

/**
 * Acquire exclusive voice or playback ownership.
 * Returns numeric sessionId on success, or null if blocked/in-flight.
 * When a new recording source requests ownership while playback is running, playback stops cleanly first.
 */
export function acquire(source, opts = {}) {
  wireNativeBridgeOnce();
  if (!VALID_SOURCES.has(source)) return null;

  // Stop active voice playback cleanly if user starts recording.
  if (source !== 'playback' && activeSession?.source === 'playback') {
    actions.stopActiveAudio?.();
    releasePlayback();
  }
  // Strict B4 mutex: never allow two concurrent recording/voice sessions (tweb#728).
  if (activeSession || inFlightStop) {
    if (!inFlightStop && opts.preempt && activeSession && activeSession.source !== source) {
      cancel(activeSession.id, { reason: 'preempted' });
    } else {
      return null;
    }
  }

  const id = ++sessionSeq;
  if (source === 'playback') {
    activeSession = { id, source: 'playback', native: false, stopped: false, opts };
    state = 'PLAYBACK';
    activeSource = 'playback';
    emitState();
    return id;
  }

  const useNative = Boolean(opts.preferNative !== false && isNativeSpeechAvailable?.() && mwSpeech?.start);
  const sess = {
    id,
    nativeId: `mw-v-${id}`,
    source,
    native: useNative,
    rec: null,
    opts,
    startedAt: performance.now(),
    silentTimer: 0,
    capTimer: 0,
    rmsRaf: 0,
    heardAudio: false,
    stopped: false,
    finalPayload: null,
    resolveFinal: null,
  };
  activeSession = sess;
  state = 'ARMING';
  activeSource = source;
  setIdleWake(true);
  emitState();

  // Start hardware engine asynchronously while holding the synchronous ARMING lock.
  (async () => {
    try {
      if (useNative) {
        const perm = await ensureNativePermission(opts);
        if (!perm.granted || activeSession !== sess || sess.stopped) {
          if (activeSession === sess) finishRelease(sess);
          return;
        }
        await mwSpeech.start({
          sessionId: sess.nativeId,
          locale: opts.locale || 'zh-CN',
          mode: opts.mode || (source === 'chat_dictate' ? 'dictation' : 'capture'),
          saveAudio: opts.saveAudio !== false,
        });
        if (activeSession !== sess || sess.stopped) {
          try { await mwSpeech.cancel?.({ sessionId: sess.nativeId }); } catch { /* ignore */ }
          return;
        }
      } else {
        const rec = createRecorder();
        sess.rec = rec;
        await rec.start();
        if (activeSession !== sess || sess.stopped) {
          rec.discard();
          return;
        }
        startWebRmsLoop(sess);
      }
      state = 'LISTENING';
      startSilentWatchdog(sess);
      emitState();
      opts.onReady?.(id);
    } catch (err) {
      const wasStopped = sess.stopped || activeSession !== sess;
      if (activeSession === sess) finishRelease(sess);
      const kind = err instanceof MicError ? err.kind : (err?.code || 'unsupported');
      if (!wasStopped && kind !== 'aborted') {
        haptic('warning');
        opts.onError?.({ kind, error: err, message: err?.message });
      }
    }
  })();

  return id;
}

function finishRelease(sess) {
  clearTimers(sess);
  sess.stopped = true;
  if (activeSession === sess) {
    activeSession = null;
    state = 'IDLE';
    activeSource = 'none';
    setIdleWake(false);
    emitState();
  }
}

/** Stop active recording and return captured payload { sessionId, durationMs, native, blob?, mime?, transcript?, draftId?, audioUrl? }. */
export async function stop(sessionId, meta = {}) {
  const sess = activeSession;
  if (!sess || (sessionId != null && sess.id !== sessionId)) return null;
  if (sess.source === 'playback') {
    releasePlayback();
    return { sessionId: sess.id, source: 'playback' };
  }
  if (sess.stopped) return inFlightStop;

  sess.stopped = true;
  clearTimers(sess);
  state = 'STOPPING';
  emitState();

  const durationMs = Math.max(0, Math.round(performance.now() - sess.startedAt));
  const stopPromise = (async () => {
    try {
      if (sess.native) {
        const finalWait = sess.finalPayload
          ? Promise.resolve(sess.finalPayload)
          : new Promise((resolve) => {
              sess.resolveFinal = resolve;
              setTimeout(() => {
                if (sess.resolveFinal) {
                  sess.resolveFinal = null;
                  resolve(sess.finalPayload || {});
                }
              }, 1600);
            });
        const stopRes = await Promise.resolve(mwSpeech.stop?.({ sessionId: sess.nativeId })).catch(() => ({}));
        const fin = await finalWait;
        const transcript = String(fin?.transcript || fin?.text || stopRes?.transcript || '').trim();
        return {
          sessionId: sess.id,
          source: sess.source,
          durationMs,
          native: true,
          transcript,
          draftId: fin?.draftId || stopRes?.draftId || null,
          audioUrl: fin?.audioUrl || stopRes?.audioUrl || null,
          mime: fin?.mime || 'audio/mp4',
          interrupted: Boolean(meta.interrupted),
          autoCap: Boolean(meta.autoCap),
        };
      }
      const { blob, mime } = sess.rec ? await sess.rec.stop() : { blob: null, mime: '' };
      return {
        sessionId: sess.id,
        source: sess.source,
        durationMs,
        native: false,
        blob,
        mime,
        transcript: '',
        interrupted: Boolean(meta.interrupted),
        autoCap: Boolean(meta.autoCap),
      };
    } finally {
      finishRelease(sess);
      inFlightStop = null;
    }
  })();

  inFlightStop = stopPromise;
  return stopPromise;
}

/** Immediately abort and discard active recording with zero leaked hardware resources. */
export function cancel(sessionId, meta = {}) {
  const sess = activeSession;
  if (!sess || (sessionId != null && sess.id !== sessionId)) return false;
  if (sess.source === 'playback') {
    releasePlayback();
    return true;
  }
  sess.stopped = true;
  clearTimers(sess);
  if (sess.resolveFinal) {
    const res = sess.resolveFinal;
    sess.resolveFinal = null;
    res({ cancelled: true });
  }
  if (sess.native) {
    try { mwSpeech?.cancel?.({ sessionId: sess.nativeId }); } catch { /* ignore */ }
  } else {
    try { sess.rec?.discard?.(); } catch { /* ignore */ }
  }
  finishRelease(sess);
  sess.opts?.onCancel?.(meta);
  return true;
}

export function releasePlayback() {
  if (activeSession?.source === 'playback') {
    finishRelease(activeSession);
  }
}

export const voiceFsm = {
  getState,
  acquire,
  stop,
  cancel,
  releasePlayback,
  subscribe: subscribeVoiceFsm,
};
