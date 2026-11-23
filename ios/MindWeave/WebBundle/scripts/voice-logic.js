// voice-logic.js — pure decision logic for the split-arc voice gesture (unit-tested).

export const MIN_HOLD_MS = 300;
export const MAX_HOLD_MS = 120000;
export const CANCEL_RADIUS = 188;
export const DEAD_ZONE = 26;

/**
 * Zone from the drag vector relative to the orb centre.
 * dx > 0 = right, upY > 0 = up. Up arc (46°..145°) = global, far = cancel, otherwise current.
 * In the 记繁琐 directory there is no "current person", so non-cancel always means global.
 */
export function computeZone(dx, upY, { directory = false } = {}) {
  const dist = Math.hypot(dx, upY);
  if (dist > CANCEL_RADIUS) return 'cancel';
  if (dist > DEAD_ZONE) {
    const angle = (Math.atan2(upY, dx) * 180) / Math.PI;
    if (angle > 46 && angle <= 145) return 'global';
  }
  return directory ? 'global' : 'current';
}

/** Map a zone to the backend routing payload. Never silently files to an arbitrary person. */
export function routeFor(zone, { activePersonId = null, directory = false } = {}) {
  if (zone === 'cancel') return null;
  if (zone === 'global' || directory || !activePersonId) return { routeMode: 'global', activePersonId: null };
  return { routeMode: 'current', activePersonId };
}

/** What to do on release. */
export function decideRelease({ zone, durationMs }) {
  if (zone === 'cancel') return 'cancel';
  if (!(durationMs >= MIN_HOLD_MS)) return 'too-short';
  return 'submit';
}

export function shouldAutoStop(elapsedMs) {
  return elapsedMs >= MAX_HOLD_MS;
}

/** 0:07 · 1:05 · 2:00 */
export function formatTimer(ms) {
  const total = Math.max(0, Math.floor((Number(ms) || 0) / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

const MIME_CANDIDATES = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus', 'audio/ogg'];

/** Pick the first supported recorder mime type ('' lets the browser choose). */
export function pickMimeType(isTypeSupported) {
  if (typeof isTypeSupported !== 'function') return '';
  for (const t of MIME_CANDIDATES) {
    try {
      if (isTypeSupported(t)) return t;
    } catch {
      /* ignore */
    }
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

/** RMS level 0..1 from AnalyserNode byte time-domain data. */
export function levelFromTimeDomain(bytes) {
  if (!bytes || !bytes.length) return 0;
  let sum = 0;
  for (let i = 0; i < bytes.length; i++) {
    const v = (bytes[i] - 128) / 128;
    sum += v * v;
  }
  return Math.min(1, Math.sqrt(sum / bytes.length) * 3.2);
}
