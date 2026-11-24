// native-bridge.js — Unified iOS WKWebView Bridge (mwNativeUI, mwSpeech, mwBiometrics, CoreHaptics,
// binary base64 shareFileBlob, window.__mwNativeEvent router) with full Web fallbacks (<= 395L).

const API_BASE_KEY = 'mw.apiBase';
const KB_CURVE_7 = 'cubic-bezier(0.38, 0.7, 0.125, 1)';
const VALID_HAPTICS = new Set([
  'light', 'medium', 'heavy', 'soft', 'rigid', 'selection', 'success', 'warning', 'error',
  'porcelainTick', 'yearDetent', 'voiceArm', 'voiceCancelCliff', 'voiceRelease', 'memorySeal', 'gentleWarn',
]);
const VIBRATE_MS = {
  light: 8, medium: 14, heavy: 20, soft: 6, rigid: 10, selection: 6,
  porcelainTick: 6, yearDetent: 12, voiceArm: 10, voiceCancelCliff: [8, 22, 10], voiceRelease: 8,
  memorySeal: [8, 28, 12], gentleWarn: [12, 32, 12], success: [10, 38, 14], warning: [16, 36, 16], error: [22, 32, 22],
};

let initialized = false;
let facade = null;
let reqSeq = 1;
let nativeKbDriven = false;
const listenersByType = new Map();
const pendingReqs = new Map();
const hasWin = () => typeof window !== 'undefined';
const nextReqId = () => `mw_${Date.now().toString(36)}_${reqSeq++}`;

function postIos(name, payload) {
  if (!hasWin()) return false;
  try {
    const h = window.webkit?.messageHandlers?.[name];
    if (h && typeof h.postMessage === 'function') { h.postMessage(payload ?? {}); return true; }
  } catch { /* ignore webkit errors */ }
  return false;
}

const externalNative = () => (hasWin() && window.MWNative && window.MWNative !== facade ? window.MWNative : null);

function invokeNativeRpc(channel, payload, timeoutMs = 12000) {
  const reqId = nextReqId();
  return new Promise((resolve) => {
    const timer = setTimeout(() => { pendingReqs.delete(reqId); resolve({ ok: false, error: 'timeout' }); }, timeoutMs);
    pendingReqs.set(reqId, (res) => { clearTimeout(timer); resolve(res); });
    if (!postIos(channel, { ...payload, reqId })) { clearTimeout(timer); pendingReqs.delete(reqId); resolve({ ok: false, error: 'unavailable' }); }
  });
}

function applyKeyboardVars(height, durationMs, curveCss) {
  if (typeof document === 'undefined') return;
  const root = document.documentElement;
  const px = Math.max(0, Math.round(Number(height) || 0));
  root.style.setProperty('--mw-kb-inset', `${px}px`);
  root.style.setProperty('--kb-inset', `${px}px`);
  if (durationMs != null && Number.isFinite(Number(durationMs))) root.style.setProperty('--mw-kb-dur', `${Math.max(0, Math.round(Number(durationMs)))}ms`);
  root.style.setProperty('--mw-kb-curve', (typeof curveCss === 'string' && curveCss.trim()) ? curveCss.trim() : KB_CURVE_7);
  root.classList.toggle('is-kb-open', px > 0);
}

function applyA11yVars(ev) {
  if (typeof document === 'undefined' || !ev) return;
  const root = document.documentElement;
  const scale = Number(ev.fontScale ?? ev.dynamicTypeScale);
  if (Number.isFinite(scale) && scale > 0) {
    const clamped = Math.max(0.85, Math.min(2.0, scale));
    const large = clamped >= 1.3;
    root.style.setProperty('--mw-dynamic-type-scale', String(clamped));
    root.classList.toggle('mw-dynamic-type-large', large);
    root.classList.toggle('mw-large-type', large);
    root.classList.toggle('is-large-type', large);
    if (root.dataset) root.dataset.largeType = large ? 'true' : 'false';
  }
  if (typeof ev.reduceMotion === 'boolean') root.classList.toggle('mw-reduce-motion', ev.reduceMotion);
  if (typeof ev.voiceOver === 'boolean') root.classList.toggle('mw-voiceover', ev.voiceOver);
}

const normalizeEventType = (raw) => { const t = String(raw || '').trim(); return t === 'ui.scrollToTop' ? 'statusbar.tap' : t; };

function dispatchNativeEvent(ev) {
  if (!ev || typeof ev !== 'object') return;
  const type = normalizeEventType(ev.type);
  const payload = type !== ev.type ? { ...ev, type } : ev;
  if (type === 'keyboard.change') { nativeKbDriven = true; applyKeyboardVars(payload.height ?? 0, payload.durationMs ?? 250, payload.curveCss); }
  else if (type === 'a11y.change') applyA11yVars(payload);
  if (payload.reqId && pendingReqs.has(payload.reqId)) {
    const resolver = pendingReqs.get(payload.reqId);
    pendingReqs.delete(payload.reqId);
    try { resolver(payload); } catch { /* ignore */ }
  }
  const exact = listenersByType.get(type);
  if (exact) for (const fn of [...exact]) { try { fn(payload); } catch { /* ignore */ } }
  const wild = listenersByType.get('*');
  if (wild) for (const fn of [...wild]) { try { fn(payload); } catch { /* ignore */ } }
}

export function onNativeEvent(type, handler, { signal } = {}) {
  const key = normalizeEventType(type);
  if (!key || typeof handler !== 'function') return () => {};
  if (!listenersByType.has(key)) listenersByType.set(key, new Set());
  listenersByType.get(key).add(handler);
  const off = () => { const set = listenersByType.get(key); if (set) { set.delete(handler); if (set.size === 0) listenersByType.delete(key); } };
  if (signal) { if (signal.aborted) off(); else signal.addEventListener('abort', off, { once: true }); }
  return off;
}

export function isNativeIos() {
  if (!hasWin()) return false;
  try {
    const mh = window.webkit?.messageHandlers;
    return Boolean(mh?.mwHaptic || mh?.mwNativeUI || mh?.mwBridge || window.__MW_IOS_NATIVE__ || window.__MW_NATIVE_IOS__ || window.location?.protocol === 'mindweave:');
  } catch { return false; }
}

export const isDebugBuild = () => Boolean(hasWin() && window.__MW_DEBUG_BUILD__ === true);
export const isNativeSpeechAvailable = () => Boolean(hasWin() && (window.__MW_SPEECH_NATIVE__ === true || window.webkit?.messageHandlers?.mwSpeech));

export function prefersReducedMotion() {
  try {
    if (typeof document !== 'undefined' && document.documentElement?.classList?.contains('mw-reduce-motion')) return true;
    return Boolean(((hasWin() && window.matchMedia) || globalThis.matchMedia)?.('(prefers-reduced-motion: reduce)')?.matches);
  } catch { return false; }
}

function normalizeApiBase(url) {
  const raw = String(url ?? '').trim();
  return !raw || !/^https?:\/\//i.test(raw) ? '' : raw.replace(/\/+$/, '').replace(/\/v1$/i, '');
}

export function getApiBase() {
  if (!hasWin()) return '';
  try {
    if (window.location?.protocol === 'mindweave:' || isNativeIos()) return '';
    return normalizeApiBase(window.localStorage?.getItem(API_BASE_KEY)) || normalizeApiBase(window.__MW_API_BASE__) || '';
  } catch { return ''; }
}

export function getConfiguredServerUrl() {
  try { return hasWin() ? String(window.__MW_SERVER_URL__ || '').trim() : ''; } catch { return ''; }
}

export function openServerConfig() {
  if (!isDebugBuild() && isNativeIos()) return false;
  if (postIos('mwConfig', { action: 'open' })) return true;
  const ext = externalNative();
  if (typeof ext?.openServerConfig === 'function') { try { ext.openServerConfig(); return true; } catch { /* ignore */ } }
  return false;
}

export function setAudioSession(action = 'playAndRecord') {
  const act = String(action || 'playAndRecord');
  if (postIos('mwAudio', { action: act })) return true;
  const ext = externalNative();
  if (typeof ext?.setAudioSession === 'function') { try { ext.setAudioSession(act); return true; } catch { /* ignore */ } }
  return false;
}

export function setApiBase(url) {
  const clean = normalizeApiBase(url);
  if (hasWin()) {
    try { if (clean) window.localStorage?.setItem(API_BASE_KEY, clean); else window.localStorage?.removeItem(API_BASE_KEY); } catch { /* private mode */ }
    postIos('mwConfig', { apiBase: clean });
    try { externalNative()?.setApiBase?.(clean); } catch { /* ignore */ }
  }
  return clean;
}

export function hapticPrepare(style = 'porcelainTick') {
  return postIos('mwHaptic', { action: 'prepare', prepare: true, style: VALID_HAPTICS.has(style) ? style : 'light' });
}

export function haptic(style = 'light', opts = {}) {
  const kind = VALID_HAPTICS.has(style) ? style : 'light';
  const payload = { style: kind };
  if (opts && typeof opts.intensity === 'number') payload.intensity = opts.intensity;
  if (opts && opts.tier) payload.tier = String(opts.tier);
  if (postIos('mwHaptic', payload)) return true;
  const ext = externalNative();
  if (typeof ext?.haptic === 'function') { try { ext.haptic(kind, opts); return true; } catch { /* ignore */ } }
  if (typeof navigator !== 'undefined' && typeof navigator.vibrate === 'function') { try { navigator.vibrate(VIBRATE_MS[kind] || 8); return true; } catch { /* ignore */ } }
  return false;
}

export async function copyText(text) {
  const value = String(text ?? '');
  if (postIos('mwClipboard', { text: value })) return true;
  const ext = externalNative();
  if (typeof ext?.copyText === 'function') { try { ext.copyText(value); return true; } catch { /* ignore */ } }
  if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) { try { await navigator.clipboard.writeText(value); return true; } catch { /* fallback */ } }
  if (typeof document === 'undefined' || typeof document.body?.appendChild !== 'function') return false;
  try {
    const ta = document.createElement('textarea');
    ta.value = value; ta.setAttribute('readonly', ''); ta.style.cssText = 'position:fixed;top:-9999px;opacity:0';
    document.body.appendChild(ta); ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    return Boolean(ok);
  } catch { return false; }
}

export async function sharePayload({ title = '心络', text = '', filename } = {}) {
  const payload = { title: String(title || '心络'), text: String(text || ''), filename: filename ? String(filename) : undefined };
  if (postIos('mwShare', payload)) return { ok: true, mode: 'ios' };
  if (typeof navigator !== 'undefined' && typeof navigator.share === 'function') {
    try { await navigator.share({ title: payload.title, text: payload.text }); return { ok: true, mode: 'web' }; }
    catch (err) { if (err && err.name === 'AbortError') return { ok: false, mode: 'cancelled' }; }
  }
  return { ok: await copyText(payload.text || payload.title), mode: 'clipboard' };
}

function bytesToBase64(bytes) {
  let bin = '';
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}

function base64ToBlob(b64, mimeType) {
  try {
    const bin = atob(b64);
    const arr = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
    return new Blob([arr], { type: mimeType || 'image/jpeg' });
  } catch { return null; }
}

export async function shareFileBlob(blob, filename = 'mindweave_backup.zip', { title, subtitle, mimeType } = {}) {
  if (!blob) return { ok: false, mode: 'empty' };
  const safeName = String(filename || 'mindweave_backup.zip');
  const type = mimeType || blob.type || 'application/octet-stream';
  if (isNativeIos() && hasWin() && window.webkit?.messageHandlers?.mwShare) {
    postIos('mwShare', { base64: bytesToBase64(new Uint8Array(await blob.arrayBuffer())), filename: safeName, mimeType: type, title: String(title || safeName), subtitle: subtitle ? String(subtitle) : undefined });
    return { ok: true, mode: 'ios' };
  }
  if (typeof navigator !== 'undefined' && typeof navigator.share === 'function' && typeof File !== 'undefined') {
    try {
      const file = new File([blob], safeName, { type });
      if (!navigator.canShare || navigator.canShare({ files: [file] })) { await navigator.share({ title: String(title || safeName), files: [file] }); return { ok: true, mode: 'web-share' }; }
    } catch { /* fall through */ }
  }
  if (typeof document === 'undefined' || typeof URL === 'undefined' || typeof document.body?.appendChild !== 'function') return { ok: false, mode: 'unsupported' };
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a'); a.href = url; a.download = safeName; a.style.display = 'none';
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
  return { ok: true, mode: 'download' };
}

export const mwSpeech = {
  async checkPermissions() {
    if (!isNativeSpeechAvailable()) return { ok: true, granted: true, mic: 'granted', speech: 'granted', engine: 'web' };
    return invokeNativeRpc('mwSpeech', { action: 'checkPermissions' }, 5000);
  },
  warm: () => postIos('mwSpeech', { action: 'warm' }),
  start: (opts = {}) => postIos('mwSpeech', { action: 'start', ...opts }),
  stop: (opts = {}) => postIos('mwSpeech', { action: 'stop', ...opts }),
  cancel: (opts = {}) => postIos('mwSpeech', { action: 'cancel', ...opts }),
  acknowledgeDraftUploaded: (localPath) => postIos('mwSpeech', { action: 'acknowledgeDraftUploaded', localPath }),
};

export const mwBiometrics = {
  async getStatus() {
    if (!isNativeIos() || !window.webkit?.messageHandlers?.mwBiometrics) return { ok: true, available: false, enabled: false, locked: false, biometryKind: 'none', labelCn: '生物识别', graceSeconds: 60 };
    return invokeNativeRpc('mwBiometrics', { action: 'getStatus' }, 5000);
  },
  async setEnabled(enabled, opts = {}) {
    if (!isNativeIos() || !window.webkit?.messageHandlers?.mwBiometrics) return { ok: false, available: false, enabled: false, error: 'unavailable' };
    return invokeNativeRpc('mwBiometrics', { action: 'setEnabled', enabled: Boolean(enabled), ...opts }, 20000);
  },
  async setGrace(graceSeconds) {
    if (!isNativeIos() || !window.webkit?.messageHandlers?.mwBiometrics) return { ok: false, available: false, graceSeconds: Number(graceSeconds) || 60 };
    return invokeNativeRpc('mwBiometrics', { action: 'setGrace', graceSeconds: Number(graceSeconds) || 0 }, 5000);
  },
};

function packPhotoResult(list, meta = {}) {
  const arr = Array.isArray(list) ? list.slice() : [];
  Object.defineProperties(arr, {
    ok: { value: meta.ok !== undefined ? Boolean(meta.ok) : arr.length > 0, enumerable: true },
    cancelled: { value: Boolean(meta.cancelled), enumerable: true },
    mode: { value: meta.mode || 'web', enumerable: true },
    images: { value: arr, enumerable: true },
    files: { value: arr, enumerable: true },
  });
  return arr;
}

function webPickPhotoFallback(maxCount) {
  if (typeof document === 'undefined' || typeof document.body?.appendChild !== 'function') return Promise.resolve(packPhotoResult([], { ok: true, mode: 'web' }));
  return new Promise((resolve) => {
    const input = document.createElement('input');
    input.type = 'file'; input.accept = 'image/*';
    if (maxCount > 1) input.multiple = true;
    input.style.display = 'none';
    let settled = false;
    const done = (files, cancelled) => { if (settled) return; settled = true; input.remove(); resolve(packPhotoResult(files, { ok: !cancelled, cancelled, mode: 'web' })); };
    input.addEventListener('change', () => done(Array.from(input.files || []).slice(0, maxCount).map((file) => ({ file, name: file.name, type: file.type })), false));
    input.addEventListener('cancel', () => done([], true));
    document.body.appendChild(input);
    input.click();
  });
}

function webShowActionSheet({ title, message, items }) {
  const host = typeof document !== 'undefined' ? (document.getElementById?.('app') || document.body) : null;
  const probe = typeof document !== 'undefined' && typeof document.createElement === 'function' ? document.createElement('div') : null;
  if (!host || typeof host.appendChild !== 'function' || !probe || typeof probe.appendChild !== 'function' || typeof probe.append !== 'function') {
    const primary = items.find((it) => it.role === 'destructive') || items.find((it) => it.role !== 'cancel');
    const ok = hasWin() && typeof window.confirm === 'function' ? window.confirm(String(title || message || '确认操作？')) : false;
    return Promise.resolve({ selectedId: ok ? (primary?.id ?? null) : null });
  }
  return new Promise((resolve) => {
    const layer = document.createElement('div'); layer.className = 'mw-sheet-layer is-mounted is-open';
    const scrim = document.createElement('div'); scrim.className = 'mw-sheet-scrim';
    const sheet = document.createElement('div'); sheet.className = 'mw-sheet';
    const handle = document.createElement('div'); handle.className = 'mw-sheet-handle';
    const body = document.createElement('div'); body.className = 'mw-sheet-body';
    if (title) { const hEl = document.createElement('h3'); hEl.className = 'mw-sheet-title'; hEl.textContent = String(title); body.appendChild(hEl); }
    if (message) { const pEl = document.createElement('p'); pEl.className = 'peek-prose'; pEl.textContent = String(message); body.appendChild(pEl); }
    const list = document.createElement('div'); list.className = 'mw-action-sheet-list';
    const finish = (selectedId) => { layer.remove(); resolve({ selectedId }); };
    for (const raw of items) {
      const btn = document.createElement('button'); btn.type = 'button';
      btn.className = `mw-action-sheet-item${raw.role === 'destructive' ? ' is-destructive' : ''}`;
      btn.textContent = String(raw.label || raw.title || '');
      btn.addEventListener('click', () => finish(raw.role === 'cancel' ? null : (raw.id ?? null)));
      list.appendChild(btn);
    }
    scrim.addEventListener('click', () => finish(null));
    body.appendChild(list); sheet.append(handle, body); layer.append(scrim, sheet);
    host.appendChild(layer);
  });
}

function webPreviewLightbox(url, title) {
  const host = typeof document !== 'undefined' ? (document.getElementById?.('app') || document.body) : null;
  if (!host || typeof host.appendChild !== 'function') return { ok: true, mode: 'web' };
  const box = document.createElement('div'); box.className = 'mw-lightbox';
  const img = document.createElement('img'); img.src = String(url); img.alt = String(title || '手帐照片');
  const closeBtn = document.createElement('button'); closeBtn.type = 'button'; closeBtn.className = 'mw-btn mw-btn-secondary'; closeBtn.textContent = '收起照片';
  const close = () => box.remove();
  closeBtn.addEventListener('click', close);
  box.addEventListener('click', (e) => { if (e.target === box) close(); });
  box.append(img, closeBtn); host.appendChild(box);
  return { ok: true, mode: 'web' };
}

export const mwNativeUI = {
  async pickPhoto({ source = 'library', maxCount = 4, maxPixelSize = 1600, tier, onWebFallback } = {}) {
    if (tier === 'silver') return packPhotoResult([], { ok: false, mode: 'blocked' });
    if (isNativeIos() && window.webkit?.messageHandlers?.mwNativeUI) {
      const res = await invokeNativeRpc('mwNativeUI', { action: 'pickPhoto', source, maxCount, maxPixelSize }, 60000);
      const raw = Array.isArray(res?.images) ? res.images : (Array.isArray(res?.files) ? res.files : []);
      const hydrated = raw.map((it) => (it && it.base64 && !it.file ? { ...it, file: base64ToBlob(it.base64, it.mimeType) } : it));
      return packPhotoResult(hydrated, { ok: Boolean(res?.ok), cancelled: Boolean(res?.cancelled), mode: 'ios' });
    }
    if (typeof onWebFallback === 'function') {
      const fb = await onWebFallback({ source, maxCount, maxPixelSize, tier });
      if (fb && typeof fb === 'object' && !Array.isArray(fb)) return packPhotoResult(fb.images || fb.files || [], fb);
      return packPhotoResult(fb, { ok: true, mode: 'web' });
    }
    return webPickPhotoFallback(maxCount);
  },
  async previewMedia({ url, title, tier, onWebFallback } = {}) {
    if (!url) return { ok: false, blocked: 'empty', mode: 'blocked' };
    if (tier === 'silver') return typeof onWebFallback === 'function' ? onWebFallback(url) : { ok: false, blocked: 'silver', mode: 'blocked' };
    if (isNativeIos() && postIos('mwNativeUI', { action: 'previewMedia', url: String(url), title: String(title || '') })) return { ok: true, mode: 'ios' };
    if (typeof onWebFallback === 'function') return onWebFallback(url);
    return webPreviewLightbox(url, title);
  },
  openSafari({ url } = {}) {
    const clean = String(url || '').trim();
    if (!clean) return { ok: false, mode: 'blocked' };
    if (/^(tel|mailto|sms):/i.test(clean)) { if (hasWin() && window.location) window.location.href = clean; return { ok: true, mode: 'direct-scheme' }; }
    if (!/^https?:\/\//i.test(clean)) return { ok: false, mode: 'blocked' };
    if (isNativeIos() && postIos('mwNativeUI', { action: 'openSafari', url: clean })) return { ok: true, mode: 'ios-safari' };
    if (hasWin() && typeof window.open === 'function') window.open(clean, '_blank', 'noopener,noreferrer');
    return { ok: true, mode: 'web' };
  },
  async showSheet({ style = 'actionSheet', title, message, items = [] } = {}) {
    const normalized = (items || []).map((it) => ({
      id: it.id, label: String(it.label ?? it.title ?? ''),
      role: it.role || (it.destructive ? 'destructive' : it.cancel ? 'cancel' : 'default'), sfSymbol: it.sfSymbol,
    }));
    if (isNativeIos() && window.webkit?.messageHandlers?.mwNativeUI) return invokeNativeRpc('mwNativeUI', { action: 'showSheet', style, title, message, items: normalized }, 60000);
    return webShowActionSheet({ title, message, items: normalized });
  },
  async showContextMenu({ rect, cornerRadius = 24, previewText, items = [], tier } = {}) {
    const filtered = (items || []).filter((it) => !(tier === 'silver' && (it.id === 'share' || it.kind === 'share' || it.shareOnly)));
    if (isNativeIos() && window.webkit?.messageHandlers?.mwNativeUI) return invokeNativeRpc('mwNativeUI', { action: 'showContextMenu', rect, cornerRadius, previewText, items: filtered }, 60000);
    return mwNativeUI.showSheet({ title: previewText ? String(previewText).slice(0, 24) : undefined, items: filtered });
  },
  async requestNotificationPermission() {
    if (isNativeIos() && window.webkit?.messageHandlers?.mwNativeUI) return invokeNativeRpc('mwNativeUI', { action: 'requestNotificationPermission' }, 30000);
    if (typeof Notification !== 'undefined' && typeof Notification.requestPermission === 'function') {
      const status = await Notification.requestPermission();
      return { ok: true, granted: status === 'granted', authorized: status === 'granted', status };
    }
    return { ok: false, granted: false, authorized: false, status: 'unsupported' };
  },
  async syncReminders(items = []) {
    const tsOf = (r) => { const t = Date.parse(String(r?.fireAtIso || r?.nextFireAt || r?.onceDate || '')); return Number.isNaN(t) ? Number.MAX_SAFE_INTEGER : t; };
    const trimmed = [...(Array.isArray(items) ? items : [])].sort((a, b) => tsOf(a) - tsOf(b)).slice(0, 48).map((r) => ({
      ...r, hiddenPreviewsBodyPlaceholder: '你有一条心络提醒', badge: null,
    }));
    if (isNativeIos() && window.webkit?.messageHandlers?.mwNativeUI) return invokeNativeRpc('mwNativeUI', { action: 'syncReminders', items: trimmed }, 10000);
    return { ok: true, mode: 'web', scheduledCount: 0 };
  },
  async copySensitive(input) {
    const value = typeof input === 'object' && input !== null ? String(input.text ?? '') : String(input ?? '');
    const ttl = typeof input === 'object' && Number.isFinite(input?.expiresInSeconds) ? input.expiresInSeconds : 300;
    if (postIos('mwClipboard', { text: value, sensitive: true, localOnly: true, expiresInSeconds: ttl })) return true;
    return copyText(value);
  },
  setIdleTimer: (disabled) => postIos('mwNativeUI', { action: 'setIdleTimer', disabled: Boolean(disabled) }),
};

export function initNativeBridge() {
  if (initialized || !hasWin() || typeof document === 'undefined') return;
  initialized = true;
  window.__mwNativeEvent = dispatchNativeEvent;
  document.documentElement.classList.toggle('is-native-ios', isNativeIos());
  applyKeyboardVars(0, 250, KB_CURVE_7);
  if (window.visualViewport) {
    const syncVv = () => {
      if (nativeKbDriven || isNativeIos()) return;
      const vv = window.visualViewport;
      const raw = Math.max(0, Math.round(window.innerHeight - vv.height - vv.offsetTop));
      applyKeyboardVars(raw >= 60 ? raw : 0, 220, KB_CURVE_7);
    };
    window.visualViewport.addEventListener('resize', syncVv, { passive: true });
    window.visualViewport.addEventListener('scroll', syncVv, { passive: true });
  }
  facade = {
    haptic, hapticPrepare, copyText, sharePayload, shareFileBlob, isNativeIos, isDebugBuild, isNativeSpeechAvailable,
    getApiBase, setApiBase, openServerConfig, getConfiguredServerUrl, setAudioSession, prefersReducedMotion,
    onNativeEvent, mwSpeech, mwBiometrics, mwNativeUI,
  };
  if (!window.MWNative) window.MWNative = facade;
}
