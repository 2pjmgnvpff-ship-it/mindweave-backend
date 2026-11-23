// native-bridge.js — iOS WKWebView native bridge (haptics, clipboard, share, audio session,
// server config sheet, keyboard inset, reduced-motion, API base) with graceful Web fallbacks.
// Safe to import in Node unit tests (guards window/document).

const API_BASE_KEY = 'mw.apiBase';
const VALID_HAPTICS = new Set(['light', 'medium', 'heavy', 'selection', 'success', 'warning', 'error']);
const VIBRATE_MS = { light: 8, medium: 14, heavy: 22, selection: 6, success: [10, 38, 14], warning: [16, 36, 16], error: [22, 32, 22] };

let initialized = false;
let facade = null;

const hasWin = () => typeof window !== 'undefined';

function postIos(name, payload) {
  if (!hasWin()) return false;
  try {
    const handler = window.webkit?.messageHandlers?.[name];
    if (handler && typeof handler.postMessage === 'function') {
      handler.postMessage(payload ?? {});
      return true;
    }
  } catch { /* ignore webkit errors */ }
  return false;
}

function externalNative() {
  if (!hasWin()) return null;
  const n = window.MWNative;
  return n && n !== facade ? n : null;
}

export function isNativeIos() {
  if (!hasWin()) return false;
  try {
    const mh = window.webkit?.messageHandlers;
    return Boolean(
      mh?.mwHaptic ||
      mh?.mwBridge ||
      window.__MW_IOS_NATIVE__ ||
      window.__MW_NATIVE_IOS__ ||
      window.location?.protocol === 'mindweave:',
    );
  } catch { return false; }
}

export function prefersReducedMotion() {
  try {
    const mm = (hasWin() && window.matchMedia) || globalThis.matchMedia;
    return Boolean(mm?.('(prefers-reduced-motion: reduce)')?.matches);
  } catch { return false; }
}

function normalizeApiBase(url) {
  const raw = String(url ?? '').trim();
  if (!raw || !/^https?:\/\//i.test(raw)) return '';
  return raw.replace(/\/+$/, '').replace(/\/v1$/i, '');
}

/**
 * Resolve API origin. Inside iOS WKWebView (`mindweave:` / native shell), ALWAYS return ''
 * so every `/v1/*` request routes same-origin through `MindWeaveSchemeHandler` (zero CORS).
 */
export function getApiBase() {
  if (!hasWin()) return '';
  try {
    if (window.location?.protocol === 'mindweave:' || isNativeIos()) return '';
    return normalizeApiBase(window.localStorage?.getItem(API_BASE_KEY)) || normalizeApiBase(window.__MW_API_BASE__) || '';
  } catch { return ''; }
}

export function getConfiguredServerUrl() {
  if (!hasWin()) return '';
  try {
    return String(window.__MW_SERVER_URL__ || '').trim();
  } catch { return ''; }
}

export function openServerConfig() {
  if (postIos('mwConfig', { action: 'open' })) return true;
  const ext = externalNative();
  if (typeof ext?.openServerConfig === 'function') {
    try { ext.openServerConfig(); return true; } catch { /* ignore */ }
  }
  return false;
}

export function setAudioSession(action = 'playAndRecord') {
  const act = String(action || 'playAndRecord');
  if (postIos('mwAudio', { action: act })) return true;
  const ext = externalNative();
  if (typeof ext?.setAudioSession === 'function') {
    try { ext.setAudioSession(act); return true; } catch { /* ignore */ }
  }
  return false;
}

export function setApiBase(url) {
  const clean = normalizeApiBase(url);
  if (hasWin()) {
    try {
      if (clean) window.localStorage?.setItem(API_BASE_KEY, clean);
      else window.localStorage?.removeItem(API_BASE_KEY);
    } catch { /* private mode */ }
    postIos('mwConfig', { apiBase: clean });
    try { externalNative()?.setApiBase?.(clean); } catch { /* ignore */ }
  }
  return clean;
}

export function haptic(style = 'light') {
  const kind = VALID_HAPTICS.has(style) ? style : 'light';
  if (postIos('mwHaptic', { style: kind })) return true;
  const ext = externalNative();
  if (typeof ext?.haptic === 'function') {
    try { ext.haptic(kind); return true; } catch { /* ignore */ }
  }
  if (typeof navigator !== 'undefined' && typeof navigator.vibrate === 'function') {
    try { navigator.vibrate(VIBRATE_MS[kind] || 8); return true; } catch { /* ignore */ }
  }
  return false;
}

export async function copyText(text) {
  const value = String(text ?? '');
  if (postIos('mwClipboard', { text: value })) return true;
  const ext = externalNative();
  if (typeof ext?.copyText === 'function') {
    try { ext.copyText(value); return true; } catch { /* ignore */ }
  }
  if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
    try { await navigator.clipboard.writeText(value); return true; } catch { /* fallback */ }
  }
  if (typeof document === 'undefined') return false;
  try {
    const ta = document.createElement('textarea');
    ta.value = value;
    ta.setAttribute('readonly', '');
    ta.style.cssText = 'position:fixed;top:-9999px;opacity:0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    return Boolean(ok);
  } catch { return false; }
}

export async function sharePayload({ title = '心络', text = '', filename } = {}) {
  const payload = { title: String(title || '心络'), text: String(text || ''), filename: filename ? String(filename) : undefined };
  if (postIos('mwShare', payload)) return { ok: true, mode: 'ios' };
  if (typeof navigator !== 'undefined' && typeof navigator.share === 'function') {
    try { await navigator.share({ title: payload.title, text: payload.text }); return { ok: true, mode: 'web' }; } catch { return { ok: false, mode: 'cancelled' }; }
  }
  return { ok: await copyText(payload.text || payload.title), mode: 'clipboard' };
}

export function initNativeBridge() {
  if (initialized || !hasWin() || typeof document === 'undefined') return;
  initialized = true;
  const root = document.documentElement;
  root.classList.toggle('is-native-ios', isNativeIos());
  root.style.setProperty('--kb-inset', '0px');
  facade = { haptic, copyText, sharePayload, isNativeIos, getApiBase, setApiBase, openServerConfig, getConfiguredServerUrl, setAudioSession, prefersReducedMotion };
  if (!window.MWNative) window.MWNative = facade;

  const syncKb = () => {
    const vv = window.visualViewport;
    if (!vv) return;
    const raw = Math.max(0, Math.round(window.innerHeight - vv.height - vv.offsetTop));
    const kb = raw > 40 ? raw : 0;
    root.style.setProperty('--kb-inset', `${kb}px`);
    root.classList.toggle('is-kb-open', kb > 0);
  };
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', syncKb, { passive: true });
    window.visualViewport.addEventListener('scroll', syncKb, { passive: true });
  }
  window.addEventListener('resize', syncKb, { passive: true });
  syncKb();
}
