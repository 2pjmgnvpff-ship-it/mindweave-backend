// voice-logic.js — pure decision logic & physics constants for MindWeave V2 voice capture.
// Zero DOM / browser dependencies (unit-tested in Node).

export const CANCEL_DY = -72;
export const PRE_ROLL_MS = 40;
export const CAPSULE_REVEAL_MS = 160;
export const TAP_VS_HOLD_MS = 240;
export const MIN_HOLD_MS = 300;
export const MAX_HOLD_MS = 120_000;
export const SILENT_MIC_MS = 2500;
export const SILENT_DBFS = -55;
export const CANCEL_RADIUS = 188;
export const DEAD_ZONE = 26;

/**
 * Pure V2 Journal slide-up cancel detector:
 * dy = clientY - startY (negative when dragging UP). dy <= CANCEL_DY (-72px) cancels cleanly.
 */
export function isSlideUpCancel(dy) {
  return Number(dy) <= CANCEL_DY;
}

/**
 * Backward-compatible vector zone helper (dx > 0 right, upY > 0 up).
 * In V2 single-gesture UX, upward drag >= 72px cancels cleanly; otherwise submits to current/global.
 */
export function computeZone(dx, upY, { directory = false, singleCancel = true } = {}) {
  const up = Number(upY) || 0;
  if (singleCancel) {
    if (up >= Math.abs(CANCEL_DY)) return 'cancel';
    return directory ? 'global' : 'current';
  }
  const dist = Math.hypot(Number(dx) || 0, up);
  if (dist > CANCEL_RADIUS) return 'cancel';
  if (dist > DEAD_ZONE) {
    const angle = (Math.atan2(up, Number(dx) || 0) * 180) / Math.PI;
    if (angle > 46 && angle <= 145) return 'global';
  }
  return directory ? 'global' : 'current';
}

/** Map a release zone to the backend routing payload. Never silently files to an arbitrary person. */
export function routeFor(zone, { activePersonId = null, directory = false } = {}) {
  if (zone === 'cancel') return null;
  if (zone === 'global' || directory || !activePersonId) return { routeMode: 'global', activePersonId: null };
  return { routeMode: 'current', activePersonId };
}

/** Decide submit vs cancel vs too-short on finger lift. */
export function decideRelease({ zone, durationMs }) {
  if (zone === 'cancel') return 'cancel';
  if (!(Number(durationMs) >= MIN_HOLD_MS)) return 'too-short';
  return 'submit';
}

export function shouldAutoStop(elapsedMs) {
  return Number(elapsedMs) >= MAX_HOLD_MS;
}

/** Smooth 30fps Living Orb breathing scale from normalized RMS (0..1): 1.00 .. 1.18. */
export function computeRmsScale(rmsRaw) {
  const clamped = Math.max(0, Math.min(1, Number(rmsRaw) || 0));
  return Number((1 + Math.pow(clamped, 0.65) * 0.18).toFixed(4));
}

/** Approximate dBFS from linear RMS amplitude (0..1). */
export function rmsToDbfs(rmsLinear) {
  const r = Math.max(1e-6, Number(rmsLinear) || 0);
  return 20 * Math.log10(r);
}

/** 0:07 · 1:05 · 2:00 */
export function formatTimer(ms) {
  const total = Math.max(0, Math.floor((Number(ms) || 0) / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

const MIME_CANDIDATES = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus', 'audio/ogg'];

/** Pick the first supported MediaRecorder mime type ('' lets the browser choose). */
export function pickMimeType(isTypeSupported) {
  if (typeof isTypeSupported !== 'function') return '';
  for (const t of MIME_CANDIDATES) {
    try {
      if (isTypeSupported(t)) return t;
    } catch { /* ignore */ }
  }
  return '';
}

export function extensionFor(mime) {
  const m = String(mime || '').toLowerCase();
  if (m.includes('mp4') || m.includes('m4a') || m.includes('aac')) return 'm4a';
  if (m.includes('ogg')) return 'ogg';
  if (m.includes('wav')) return 'wav';
  if (m.includes('mpeg') || m.includes('mp3')) return 'mp3';
  return 'webm';
}

/** Linear RMS level (0..1) from AnalyserNode byte time-domain data. */
export function levelFromTimeDomain(bytes) {
  if (!bytes || !bytes.length) return 0;
  let sum = 0;
  for (let i = 0; i < bytes.length; i++) {
    const v = (bytes[i] - 128) / 128;
    sum += v * v;
  }
  return Math.min(1, Math.sqrt(sum / bytes.length) * 3.2);
}
