// client.js — fetch wrapper for the MindWeave API (same origin, base '/v1').
// Bearer token, single-flight refresh on 401 (retry keeps the same Idempotency-Key),
// RFC 7807 problems → warm Chinese messages. Never surfaces `detail` verbatim.
//
// TODO(P1 hardening): tokens live in localStorage for P0. Move the refresh token to an
// httpOnly SameSite cookie (or a service-worker-held secret) and keep the access token in memory.

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

const FRIENDLY = {
  network: '网络有点慢，稍后再试一次吧',
  unauthorized: '登录已过期，请重新登录',
  reauth_required: '为了安全，请重新登录一下',
  forbidden: '这里暂时不能打开',
  not_found: '这页手帐好像不见了',
  validation_failed: '这句话好像不太完整，换个说法试试？',
  bad_request: '这次没能记下，换个说法试试？',
  conflict: '刚刚有别的改动，刷新一下再试',
  version_mismatch: '刚刚有别的改动，刷新一下再试',
  idempotency_conflict: '这句话已经在路上了，稍等一下',
  payload_too_large: '这段录音有点长，分两次说试试',
  rate_limited: '说得有点快啦，歇一会儿再来',
  quota_exceeded: '今天的次数用完啦，明天再来',
  ai_upstream_error: '丘比特刚刚走神了，再试一次吧',
  undo_expired: '已经过了撤销时间',
  server: '心络这边出了点小状况，稍后再试',
};

export function friendlyMessage(code, status) {
  if (code && FRIENDLY[code]) return FRIENDLY[code];
  if (status === 401) return FRIENDLY.unauthorized;
  if (status === 404) return FRIENDLY.not_found;
  if (status === 409) return FRIENDLY.conflict;
  if (status === 413) return FRIENDLY.payload_too_large;
  if (status === 422) return FRIENDLY.validation_failed;
  if (status === 429) return FRIENDLY.rate_limited;
  if (status >= 500) return FRIENDLY.server;
  if (!status) return FRIENDLY.network;
  return FRIENDLY.bad_request;
}

export class ApiError extends Error {
  constructor({ code, status, requestId, problem }) {
    super(friendlyMessage(code, status));
    this.name = 'ApiError';
    this.code = code || (status ? `http_${status}` : 'network');
    this.status = status || 0;
    this.requestId = requestId || null;
    this.problem = problem || null;
  }
  get retryable() {
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

/**
 * @param {{fetchImpl?: typeof fetch, storage?: Storage, base?: string, onAuthLost?: () => void}} opts
 */
export function createClient(opts = {}) {
  const fetchImpl = opts.fetchImpl || ((...a) => globalThis.fetch(...a));
  const storage = opts.storage || globalThis.localStorage || memoryStore();
  const base = opts.base ?? '/v1';
  let onAuthLost = opts.onAuthLost || (() => {});
  let refreshing = null;

  function getTokens() {
    try {
      return JSON.parse(storage.getItem(TOKEN_KEY) || 'null');
    } catch {
      return null;
    }
  }
  function setTokens(t) {
    if (!t) storage.removeItem(TOKEN_KEY);
    else storage.setItem(TOKEN_KEY, JSON.stringify({ accessToken: t.accessToken, refreshToken: t.refreshToken }));
  }

  async function refreshOnce() {
    if (!refreshing) {
      refreshing = (async () => {
        const tokens = getTokens();
        if (!tokens?.refreshToken) return false;
        try {
          const res = await fetchImpl(`${base}/auth/refresh`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
            body: JSON.stringify({ refreshToken: tokens.refreshToken }),
          });
          if (!res.ok) {
            if (res.status === 401 || res.status === 400 || res.status === 403) setTokens(null);
            return false;
          }
          setTokens(await res.json());
          return true;
        } catch {
          return false;
        }
      })().finally(() => {
        setTimeout(() => (refreshing = null), 0);
      });
    }
    return refreshing;
  }

  function buildUrl(path, query) {
    let url = path.startsWith('http') ? path : `${base}${path}`;
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
    try {
      problem = await res.json();
    } catch {
      /* not json */
    }
    return new ApiError({
      code: problem?.code,
      status: res.status,
      requestId: problem?.requestId || res.headers?.get?.('X-Request-Id'),
      problem,
    });
  }

  /**
   * request('POST', '/captures', { body, query, idem, form, raw, auth, signal, headers })
   * idem: true (auto uuid) | string (explicit key) | false. Defaults to true for mutations.
   */
  async function request(method, path, o = {}) {
    const m = method.toUpperCase();
    const auth = o.auth !== false;
    const idemKey = o.idem === false ? null : typeof o.idem === 'string' ? o.idem : MUTATING.has(m) ? uuid() : null;
    const url = buildUrl(path, o.query);

    const attempt = async () => {
      const headers = { Accept: o.raw ? 'text/event-stream, application/json' : 'application/json', ...(o.headers || {}) };
      let body;
      if (o.form) body = o.form;
      else if (o.body !== undefined) {
        headers['Content-Type'] = 'application/json';
        body = JSON.stringify(o.body);
      }
      if (idemKey) headers['Idempotency-Key'] = idemKey;
      const tokens = getTokens();
      if (auth && tokens?.accessToken) headers.Authorization = `Bearer ${tokens.accessToken}`;
      try {
        return await fetchImpl(url, { method: m, headers, body, signal: o.signal });
      } catch (err) {
        if (err && err.name === 'AbortError') throw err;
        throw new ApiError({ code: 'network', status: 0 });
      }
    };

    let res = await attempt();
    if (res.status === 401 && auth && getTokens()?.refreshToken) {
      const ok = await refreshOnce();
      if (ok) res = await attempt();
    }
    if (res.status === 401 && auth) {
      const err = await toError(res);
      setTokens(null);
      onAuthLost(err);
      throw err;
    }
    if (!res.ok) throw await toError(res);
    if (o.raw) return res;
    if (res.status === 204) return null;
    const text = await res.text();
    return text ? JSON.parse(text) : null;
  }

  return {
    request,
    get: (p, o) => request('GET', p, o),
    post: (p, body, o = {}) => request('POST', p, { ...o, body }),
    patch: (p, body, o = {}) => request('PATCH', p, { ...o, body }),
    getTokens,
    setTokens,
    isLoggedIn: () => Boolean(getTokens()?.accessToken),
    setOnAuthLost: (fn) => (onAuthLost = fn),
    refreshOnce,
    idemKey: uuid,
  };
}
