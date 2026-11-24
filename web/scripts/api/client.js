// client.js — MindWeave API client (iOS 16-safe AbortSignal, TRAP-01 /v1 prefix fix,
// all 22 live RFC 7807 codes + S01.5 retryAfterSeconds interpolation, 408/409 retry, <= 270L).

import { getApiBase } from '../native-bridge.js';

const TOKEN_KEY = 'mw.auth';
const MUTATING = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

export function uuid() {
  const c = globalThis.crypto;
  if (c && typeof c.randomUUID === 'function') return c.randomUUID();
  const bytes = new Uint8Array(16);
  if (c && typeof c.getRandomValues === 'function') c.getRandomValues(bytes);
  else for (let i = 0; i < 16; i++) bytes[i] = Math.floor(Math.random() * 256);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/** iOS 16-safe signal combiner with optional timeout (ZERO AbortSignal.any). */
export function createRequestSignal(externalSignal, timeoutMs) {
  const hasTimeout = typeof timeoutMs === 'number' && Number.isFinite(timeoutMs) && timeoutMs > 0;
  if (!externalSignal && !hasTimeout) return { signal: undefined, cleanup() {} };
  if (typeof AbortController === 'undefined') return { signal: externalSignal, cleanup() {} };
  const ctrl = new AbortController();
  let timer = 0;
  const onAbort = () => ctrl.abort(externalSignal.reason);
  if (externalSignal) {
    if (externalSignal.aborted) { ctrl.abort(externalSignal.reason); return { signal: ctrl.signal, cleanup() {} }; }
    externalSignal.addEventListener('abort', onAbort, { once: true });
  }
  if (hasTimeout) timer = setTimeout(() => ctrl.abort(new DOMException('Request timed out', 'AbortError')), timeoutMs);
  return {
    signal: ctrl.signal,
    cleanup() { if (timer) clearTimeout(timer); if (externalSignal) externalSignal.removeEventListener('abort', onAbort); },
  };
}

export const FRIENDLY = Object.freeze({
  network: '网络有点慢，稍后再试一次吧',
  unauthorized: '登录已过期，请重新登录',
  reauth_required: '为了保护你的手帐，请重新验证一次身份',
  reauth_invalid: '身份验证未通过，请再试一次',
  reauth_expired: '验证已过期，请重新验证一次身份',
  apple_token_invalid: 'Apple 登录验证未通过，请再试一次',
  forbidden: '这里暂时不能打开',
  not_found: '这页手帐好像不见了',
  person_not_found: '找不到这位人物的手帐了',
  account_deleted: '这个账号已完成注销',
  validation_failed: '填写的内容还不太完整，换个说法试试？',
  request_validation_failed: '填写的内容还不太完整，请检查后再试',
  bad_request: '这次没能记下，换个说法试试？',
  conflict: '刚刚有别的改动，刷新一下再试',
  version_mismatch: '刚刚有别的改动，刷新一下再试',
  idempotency_conflict: '这句话已经在路上了，稍等一下',
  idempotency_in_progress: '上一笔记录还在处理中，请稍候片刻',
  payload_too_large: '这段录音有点长，分两次说试试',
  rate_limited: '说得有点快啦，歇一会儿再来',
  quota_exceeded: '今天的次数用完啦，明天再来',
  concurrent_limit_exceeded: '上一条回复还在进行中，请稍候片刻再试',
  ai_upstream_error: '丘比特刚刚走神了，再试一次吧',
  ai_budget_paused: '丘比特今天需要休息一会儿，手帐记录和翻看依然正常可用',
  asr_provider_unavailable: '语音转写暂时不可用，录音已安全保存在本地，稍后会自动重试',
  undo_expired: '已超过撤销时限，可以在最近归档记录里修改或删除',
  undo_window_expired: '已超过撤销时限，可以在最近归档记录里修改或删除',
  export_in_progress: '手帐备份正在打包中，请稍候',
  export_expired: '这份备份链接已过期，重新生成一份吧',
  refresh_token_replayed: '为了保护你的手帐安全，请重新登录',
  otp_cooldown_active: '发送太频繁，请稍后再试',
  otp_cooldown: '发送太频繁，请稍后再试',
  invalid_otp: '验证码不正确，请核对后再输一次',
  otp_invalid: '验证码不正确，请核对后再输一次',
  otp_expired: '验证码已过期，请重新获取一条',
  otp_already_consumed: '这条验证码已经用过了，请重新获取',
  otp_max_attempts_exceeded: '尝试次数过多，请稍后重新获取验证码',
  otp_too_many_attempts: '尝试次数过多，请稍后重新获取验证码',
  email_provider_not_configured: '邮件服务暂未就绪，请稍后再试或使用 Apple 登录',
  email_delivery_failed: '验证码邮件暂时没能发出，请稍后再试',
  email_delivery_timeout: '邮件发送超时了，请稍后再试一次',
  email_upstream_unavailable: '发信通道暂时有点忙，稍后再试一次',
  email_recipient_rejected: '这个邮箱地址无法收信，请检查是否拼写有误',
  email_provider_auth_failed: '邮件通道正在维护，请稍后再试',
  email_provider_rate_limited: '当前发送邮件的人比较多，请稍等片刻再试',
  email_provider_quota_exceeded: '今日邮件发送已达上限，请稍后再试或使用 Apple 登录',
  email_sender_sandbox_restricted: '当前邮箱暂不支持收信，请换一个常用邮箱再试',
  email_sender_domain_unverified: '发信服务正在配置中，请稍后再试',
  email_smtp_from_mismatch: '发信通道同步中，请稍后再试',
  server: '心络这边出了点小状况，稍后再试',
});

export function parseRetryAfterSeconds(headerVal, problem) {
  const fromProb = problem?.retryAfterSeconds ?? problem?.details?.retryAfterSeconds ?? problem?.retry_after_seconds;
  if (fromProb != null && Number.isFinite(Number(fromProb)) && Number(fromProb) > 0) return Math.ceil(Number(fromProb));
  if (!headerVal) return null;
  const num = Number(headerVal);
  if (Number.isFinite(num) && num > 0) return Math.ceil(num);
  const ts = Date.parse(String(headerVal));
  if (!Number.isNaN(ts)) { const sec = Math.ceil((ts - Date.now()) / 1000); return sec > 0 ? sec : null; }
  return null;
}
export function friendlyMessage(code, status, problem = null) {
  if (code === 'otp_cooldown_active' || code === 'otp_cooldown') {
    const sec = parseRetryAfterSeconds(null, problem);
    return sec ? `发送太频繁，请 ${sec} 秒后再试` : FRIENDLY.otp_cooldown_active;
  }
  if (code && FRIENDLY[code]) return FRIENDLY[code];
  const byStatus = { 401: FRIENDLY.unauthorized, 404: FRIENDLY.not_found, 408: FRIENDLY.network, 409: FRIENDLY.conflict, 413: FRIENDLY.payload_too_large, 422: FRIENDLY.validation_failed, 429: FRIENDLY.rate_limited };
  if (byStatus[status]) return byStatus[status];
  if (status >= 500) return FRIENDLY.server;
  return !status ? FRIENDLY.network : FRIENDLY.bad_request;
}

export class ApiError extends Error {
  constructor({ code, status, requestId, problem, retryAfterSeconds }) {
    const merged = retryAfterSeconds && problem && !problem.retryAfterSeconds
      ? { ...problem, retryAfterSeconds }
      : (problem || (retryAfterSeconds ? { retryAfterSeconds } : null));
    super(friendlyMessage(code, status, merged));
    this.name = 'ApiError';
    this.code = code || (status ? `http_${status}` : 'network');
    this.status = status || 0;
    this.requestId = requestId || null;
    this.problem = merged;
    this.retryAfterSeconds = retryAfterSeconds || parseRetryAfterSeconds(null, merged) || null;
  }
  get retryable() {
    if (this.status === 408) return true;
    if (this.status === 409 && this.code === 'idempotency_in_progress') return true;
    return this.status === 0 || this.status >= 500 || this.status === 429;
  }
}

export function isRetryableError(err) {
  if (!err) return false;
  if (err instanceof ApiError) return err.retryable || err.status === 401;
  return err.name === 'TypeError' || err.name === 'AbortError';
}

function memoryStore() {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k) };
}

const sanitizeMultipartToken = (v) => String(v ?? '').replace(/[\r\n"]/g, '_');

export async function serializeFormData(form) {
  const boundary = `----MWFormBoundary${uuid().replace(/-/g, '')}`;
  const enc = new TextEncoder();
  const crlf = enc.encode('\r\n');
  const chunks = [];
  for (const [rawName, val] of form.entries()) {
    const name = sanitizeMultipartToken(rawName);
    if (typeof Blob !== 'undefined' && val instanceof Blob) {
      const fname = sanitizeMultipartToken(val.name || 'upload.bin');
      const mime = sanitizeMultipartToken(val.type || 'application/octet-stream');
      chunks.push(enc.encode(`--${boundary}\r\nContent-Disposition: form-data; name="${name}"; filename="${fname}"\r\nContent-Type: ${mime}\r\n\r\n`), new Uint8Array(await val.arrayBuffer()), crlf);
    } else {
      chunks.push(enc.encode(`--${boundary}\r\nContent-Disposition: form-data; name="${name}"\r\n\r\n`), enc.encode(String(val ?? '')), crlf);
    }
  }
  chunks.push(enc.encode(`--${boundary}--\r\n`));
  const bytes = new Uint8Array(chunks.reduce((sum, c) => sum + c.byteLength, 0));
  let offset = 0;
  for (const c of chunks) { bytes.set(c, offset); offset += c.byteLength; }
  return { bytes, contentType: `multipart/form-data; boundary=${boundary}` };
}

export function createClient(opts = {}) {
  const fetchImpl = opts.fetchImpl || ((...a) => globalThis.fetch(...a));
  const storage = opts.storage || globalThis.localStorage || memoryStore();
  const explicitBase = opts.base;
  const resolveBase = () => (explicitBase !== undefined ? explicitBase : (getApiBase() ? `${getApiBase()}/v1` : '/v1'));
  let onAuthLost = opts.onAuthLost || (() => {});
  let refreshing = null;

  const getTokens = () => { try { return JSON.parse(storage.getItem(TOKEN_KEY) || 'null'); } catch { return null; } };
  const setTokens = (t) => {
    if (!t) storage.removeItem(TOKEN_KEY);
    else storage.setItem(TOKEN_KEY, JSON.stringify({ accessToken: t.accessToken, refreshToken: t.refreshToken }));
  };

  async function refreshOnce() {
    if (!refreshing) {
      refreshing = (async () => {
        const tokens = getTokens();
        if (!tokens?.refreshToken) return false;
        try {
          const res = await fetchImpl(`${resolveBase()}/auth/refresh`, {
            method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
            body: JSON.stringify({ refreshToken: tokens.refreshToken }),
          });
          if (!res.ok) { if (res.status === 401 || res.status === 400 || res.status === 403) setTokens(null); return false; }
          setTokens(await res.json());
          return true;
        } catch { return false; }
      })().finally(() => { setTimeout(() => (refreshing = null), 0); });
    }
    return refreshing;
  }

  function buildUrl(path, query) {
    const rawPath = String(path || '');
    const normalizedPath = rawPath.startsWith('/v1/') ? rawPath.slice(3) : rawPath;
    let url = /^https?:\/\//i.test(rawPath) ? rawPath : `${resolveBase()}${normalizedPath}`;
    if (query) {
      const qs = Object.entries(query)
        .filter(([, v]) => v !== undefined && v !== null && v !== '')
        .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
        .join('&');
      if (qs) url += (url.includes('?') ? '&' : '?') + qs;
    }
    return url;
  }

  async function toError(res) {
    let problem = null;
    try { problem = await res.json(); } catch { /* not json */ }
    const retryAfterSeconds = parseRetryAfterSeconds(res.headers?.get?.('Retry-After'), problem);
    return new ApiError({ code: problem?.code, status: res.status, requestId: problem?.requestId || res.headers?.get?.('X-Request-Id'), problem, retryAfterSeconds });
  }

  async function request(method, path, o = {}) {
    const m = method.toUpperCase();
    const auth = o.auth !== false;
    const idemKey = o.idem === false ? null : typeof o.idem === 'string' ? o.idem : MUTATING.has(m) ? uuid() : null;
    const url = buildUrl(path, o.query);
    const rawForm = o.form || (typeof FormData !== 'undefined' && o.body instanceof FormData ? o.body : null);
    const serializedForm = rawForm && globalThis.location?.protocol === 'mindweave:' && typeof FormData !== 'undefined' && rawForm instanceof FormData
      ? await serializeFormData(rawForm) : null;

    const attempt = async () => {
      const { signal, cleanup } = createRequestSignal(o.signal, o.timeoutMs);
      const headers = { Accept: o.raw ? (o.accept || '*/*') : 'application/json', ...(o.headers || {}) };
      let body;
      if (serializedForm) { headers['Content-Type'] = serializedForm.contentType; body = serializedForm.bytes; }
      else if (rawForm) { body = rawForm; }
      else if (o.body !== undefined) { headers['Content-Type'] = 'application/json'; body = JSON.stringify(o.body); }
      if (idemKey) headers['Idempotency-Key'] = idemKey;
      const tokens = getTokens();
      if (auth && tokens?.accessToken) headers.Authorization = `Bearer ${tokens.accessToken}`;
      try { return await fetchImpl(url, { method: m, headers, body, signal }); }
      catch (err) { if (err && err.name === 'AbortError') throw err; throw new ApiError({ code: 'network', status: 0 }); }
      finally { cleanup(); }
    };

    let res = await attempt();
    if (res.status === 401 && auth && getTokens()?.refreshToken && (await refreshOnce())) res = await attempt();
    if ((res.status === 408 || res.status === 409) && !o._retriedConflict) {
      const probeErr = await toError(res);
      if (probeErr.status === 408 || probeErr.code === 'idempotency_in_progress') {
        const delayMs = Math.min(1500, Math.max(250, (probeErr.retryAfterSeconds || 0.4) * 1000));
        await new Promise((r) => setTimeout(r, delayMs));
        res = await attempt();
      } else { throw probeErr; }
    }
    if (res.status === 401 && auth) { const err = await toError(res); setTokens(null); onAuthLost(err); throw err; }
    if (!res.ok) throw await toError(res);
    if (o.raw) return res;
    if (res.status === 204) return null;
    const text = await res.text();
    return text ? JSON.parse(text) : null;
  }

  return {
    request, buildUrl,
    get: (p, o) => request('GET', p, o),
    post: (p, body, o = {}) => request('POST', p, { ...o, body }),
    patch: (p, body, o = {}) => request('PATCH', p, { ...o, body }),
    getTokens, setTokens, isLoggedIn: () => Boolean(getTokens()?.accessToken),
    setOnAuthLost: (fn) => (onAuthLost = fn), refreshOnce, idemKey: uuid,
  };
}
