// recorder.js — getUserMedia + MediaRecorder + AnalyserNode, behind a small interface.
import { pickMimeType, levelFromTimeDomain } from './voice-logic.js';

export class MicError extends Error {
  constructor(kind, cause) {
    super(kind);
    this.kind = kind; // 'denied' | 'unsupported' | 'busy'
    this.cause = cause;
  }
}

export function recordingSupported() {
  return Boolean(globalThis.navigator?.mediaDevices?.getUserMedia && globalThis.MediaRecorder);
}

/**
 * const rec = createRecorder(); await rec.start(); rec.level(); const { blob, mime } = await rec.stop();
 * rec.discard() stops without producing a blob.
 */
export function createRecorder() {
  let stream = null;
  let recorder = null;
  let chunks = [];
  let audioCtx = null;
  let analyser = null;
  let buf = null;
  let mime = '';

  async function start() {
    if (!recordingSupported()) throw new MicError('unsupported');
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    } catch (err) {
      const name = err && err.name;
      throw new MicError(name === 'NotAllowedError' || name === 'SecurityError' ? 'denied' : name === 'NotReadableError' ? 'busy' : 'unsupported', err);
    }
    mime = pickMimeType((t) => MediaRecorder.isTypeSupported(t));
    recorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
    mime = recorder.mimeType || mime || 'audio/webm';
    chunks = [];
    recorder.ondataavailable = (e) => {
      if (e.data && e.data.size) chunks.push(e.data);
    };
    recorder.start(250);
    try {
      const Ctx = globalThis.AudioContext || globalThis.webkitAudioContext;
      audioCtx = new Ctx();
      const src = audioCtx.createMediaStreamSource(stream);
      analyser = audioCtx.createAnalyser();
      analyser.fftSize = 512;
      buf = new Uint8Array(analyser.fftSize);
      src.connect(analyser);
    } catch {
      analyser = null; // waveform falls back to a gentle idle ribbon
    }
  }

  function level() {
    if (!analyser) return 0.18;
    analyser.getByteTimeDomainData(buf);
    return levelFromTimeDomain(buf);
  }

  function cleanup() {
    try {
      stream && stream.getTracks().forEach((t) => t.stop());
    } catch {
      /* ignore */
    }
    try {
      audioCtx && audioCtx.close();
    } catch {
      /* ignore */
    }
    stream = null;
    audioCtx = null;
    analyser = null;
  }

  function stop() {
    return new Promise((resolve) => {
      if (!recorder || recorder.state === 'inactive') {
        cleanup();
        resolve({ blob: new Blob(chunks, { type: mime }), mime });
        return;
      }
      recorder.onstop = () => {
        cleanup();
        resolve({ blob: new Blob(chunks, { type: mime }), mime });
      };
      try {
        recorder.requestData();
      } catch {
        /* ignore */
      }
      recorder.stop();
    });
  }

  function discard() {
    try {
      if (recorder && recorder.state !== 'inactive') {
        recorder.onstop = null;
        recorder.stop();
      }
    } catch {
      /* ignore */
    }
    chunks = [];
    cleanup();
  }

  return { start, stop, discard, level, get mime() { return mime; } };
}
