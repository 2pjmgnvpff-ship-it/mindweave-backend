// recorder.js — Web fallback MediaRecorder with singleton AudioContext (LK-1 leak fix),
// post-getUserMedia abort guard, and native-speech mwAudio bypass.
import { pickMimeType, levelFromTimeDomain } from './voice-logic.js';
import { setAudioSession, isNativeSpeechAvailable } from './native-bridge.js';

export class MicError extends Error {
  constructor(kind, cause) {
    super(kind);
    this.kind = kind; // 'denied' | 'unsupported' | 'busy' | 'aborted'
    this.cause = cause;
  }
}

export function recordingSupported() {
  return Boolean(globalThis.navigator?.mediaDevices?.getUserMedia && globalThis.MediaRecorder);
}

// Singleton AudioContext reused across holds (02d LK-1: never leak a fresh AudioContext per press).
let sharedAudioCtx = null;

function acquireAudioContext() {
  const Ctx = globalThis.AudioContext || globalThis.webkitAudioContext;
  if (!Ctx) return null;
  if (!sharedAudioCtx || sharedAudioCtx.state === 'closed') {
    sharedAudioCtx = new Ctx();
  }
  if (sharedAudioCtx.state === 'suspended') {
    sharedAudioCtx.resume().catch(() => {});
  }
  return sharedAudioCtx;
}

function suspendAudioContext() {
  if (sharedAudioCtx && sharedAudioCtx.state === 'running') {
    sharedAudioCtx.suspend().catch(() => {});
  }
}

/**
 * const rec = createRecorder(); await rec.start(); rec.level(); const { blob, mime } = await rec.stop();
 * rec.discard() stops cleanly even if getUserMedia is still in flight.
 */
export function createRecorder() {
  let stream = null;
  let recorder = null;
  let chunks = [];
  let sourceNode = null;
  let analyser = null;
  let buf = null;
  let mime = '';
  let sessionActive = false;
  let aborted = false;

  const useNativeSession = () => !isNativeSpeechAvailable?.();

  async function start() {
    if (aborted) throw new MicError('aborted');
    if (!recordingSupported()) throw new MicError('unsupported');
    if (useNativeSession()) {
      setAudioSession?.('playAndRecord');
      sessionActive = true;
    }
    let acquired = null;
    try {
      acquired = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch (err) {
      releaseSession();
      const name = err?.name;
      const kind = name === 'NotAllowedError' || name === 'SecurityError'
        ? 'denied'
        : name === 'NotReadableError' || name === 'AbortError'
          ? 'busy'
          : 'unsupported';
      throw new MicError(kind, err);
    }
    // LK-1 post-getUserMedia abort guard: user released or cancelled while permission prompt was open!
    if (aborted) {
      stopTracks(acquired);
      releaseSession();
      throw new MicError('aborted');
    }
    stream = acquired;
    mime = pickMimeType((t) => MediaRecorder.isTypeSupported(t));
    recorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
    mime = recorder.mimeType || mime || 'audio/webm';
    chunks = [];
    recorder.ondataavailable = (e) => {
      if (e.data && e.data.size) chunks.push(e.data);
    };
    recorder.start(250);
    try {
      const ctx = acquireAudioContext();
      if (ctx) {
        sourceNode = ctx.createMediaStreamSource(stream);
        analyser = ctx.createAnalyser();
        analyser.fftSize = 512;
        buf = new Uint8Array(analyser.fftSize);
        sourceNode.connect(analyser);
      }
    } catch {
      sourceNode = null;
      analyser = null;
    }
  }

  function level() {
    if (!analyser || !buf) return 0;
    analyser.getByteTimeDomainData(buf);
    return levelFromTimeDomain(buf);
  }

  function stopTracks(s) {
    try {
      s?.getTracks?.().forEach((t) => t.stop());
    } catch { /* ignore */ }
  }

  function releaseSession() {
    if (sessionActive) {
      sessionActive = false;
      if (useNativeSession()) setAudioSession?.('deactivate');
    }
  }

  function cleanup() {
    stopTracks(stream);
    try { sourceNode?.disconnect?.(); } catch { /* ignore */ }
    try { analyser?.disconnect?.(); } catch { /* ignore */ }
    suspendAudioContext();
    releaseSession();
    stream = null;
    sourceNode = null;
    analyser = null;
    buf = null;
  }

  function stop() {
    return new Promise((resolve) => {
      if (!recorder || recorder.state === 'inactive') {
        const blob = new Blob(chunks, { type: mime });
        cleanup();
        resolve({ blob, mime });
        return;
      }
      let settled = false;
      const done = () => {
        if (settled) return;
        settled = true;
        const blob = new Blob(chunks, { type: mime });
        cleanup();
        resolve({ blob, mime });
      };
      const safety = setTimeout(done, 800);
      recorder.onstop = () => {
        clearTimeout(safety);
        done();
      };
      try { recorder.requestData(); } catch { /* ignore */ }
      try { recorder.stop(); } catch { clearTimeout(safety); done(); }
    });
  }

  function discard() {
    aborted = true;
    try {
      if (recorder && recorder.state !== 'inactive') {
        recorder.onstop = null;
        recorder.stop();
      }
    } catch { /* ignore */ }
    chunks = [];
    cleanup();
  }

  return {
    start,
    stop,
    discard,
    level,
    get mime() { return mime; },
    get aborted() { return aborted; },
  };
}
