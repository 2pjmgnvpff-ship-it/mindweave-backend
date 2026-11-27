/**
 * MindWeave (心络) v16 Lumina — Production Full-Stack Web Application
 * Wired 100% live to FastAPI + PostgreSQL 16 + Per-User AES-256-GCM Encryption
 */

const API_BASE = '/v1';
const STORAGE_SESSION_KEY = 'mw_v16_session';
const STORAGE_GUEST_EMAIL_KEY = 'mw_v16_guest_email';
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const ARROW_PINS = {
  gold: '/assets/cupid/arrow_pin_gold.webp',
  crystal: '/assets/cupid/arrow_pin_crystal.webp',
  silver: '/assets/cupid/arrow_pin_silver.webp',
};

const TIER_META = {
  gold: { label: '金箭', full: '金箭 · 记爱' },
  crystal: { label: '水晶箭', full: '水晶箭 · 记繁琐' },
  silver: { label: '银箭', full: '银箭 · 记恨' },
};

const FACT_KIND_LABELS = {
  preference: '喜好',
  taboo: '忌口',
  date: '日子',
  wish: '心愿',
  promise: '记挂',
  event: '往事',
  note: '小事',
};

const LABEL_TO_FACT_TYPE = {
  '喜好': { type: 'preference', polarity: 'positive' },
  '忌口': { type: 'taboo', polarity: 'negative' },
  '习惯': { type: 'preference', polarity: 'neutral' },
  '心法': { type: 'note', polarity: 'positive' },
  '记挂': { type: 'promise', polarity: 'positive' },
  '日期': { type: 'date', polarity: 'neutral' },
  '规矩': { type: 'taboo', polarity: 'negative' },
  '底线': { type: 'taboo', polarity: 'negative' },
};

/* ============================================================================
   0. STATE & UTILITIES
   ============================================================================ */
const state = {
  session: loadSavedSession(),
  otpChallenge: null,
  preferences: { preferredEdition: 'lumina', version: 1 },
  people: [],
  personId: null,
  inboxItems: [],
  inboxCount: 0,
  scale: 'folio', // folio | season | rings
  lighting: 'daylight', // daylight | evening
  shieldGlobalOpen: false,
  unveiledIds: new Set(),
  playingVoiceId: null,
  voiceTimer: null,
  voiceProgress: 0,
  selectedRingId: null,
  dirFilter: 'all',
  showNewPersonForm: false,
  capMode: 'voice', // voice | wechat | photo | care
  capWaveRaf: null,
  spotFilter: 'all',
  cupidMessages: {}, // personId -> array of chat message objects
  cupidLoaded: new Set(),
  cupidThinking: false,
  cupidTraceOpen: false,
  cupidAttachedIds: new Set(),
  cupidPerspectiveOpen: false,
  lbPhotos: [],
  lbIndex: 0,
  faceIdLock: true,
  wheelYear: 2026,
  wheelMonth: 12,
  wheelDay: 16,
  wheelDraftRecId: null,
  seeding: false,
  sheetOpenedAt: 0,
};

function makeUuid() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
  });
}

function escapeHtml(str) {
  return String(str || '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[c]));
}

function formatShortDate(iso) {
  if (!iso) return '今日';
  const s = String(iso);
  if (/^\d{4}-\d{2}-\d{2}/.test(s)) return `${s.slice(5, 7)}·${s.slice(8, 10)}`;
  return s.slice(0, 10);
}

function formatDaysLeft(isoDate) {
  if (!isoDate) return '近期';
  const target = new Date(`${String(isoDate).slice(0, 10)}T00:00:00`);
  if (Number.isNaN(target.getTime())) return '近期';
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const diff = Math.round((target - today) / 86400000);
  if (diff < 0) return '已过';
  if (diff === 0) return '今天';
  if (diff === 1) return '明天';
  return `${diff}天后`;
}

function leadDaysLabel(arr) {
  if (!Array.isArray(arr) || !arr.length) return '当天';
  const maxD = Math.max(...arr.map(Number));
  if (maxD >= 7) return '提前 7 天';
  if (maxD >= 1) return `提前 ${maxD} 天`;
  return '当天';
}

function currentPerson() {
  return state.people.find((p) => p.id === state.personId) || state.people[0] || null;
}

let toastTimer = null;
function showToast(msg) {
  const el = document.getElementById('toastPill');
  if (!el) return;
  el.textContent = msg;
  el.classList.add('is-show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('is-show'), 2800);
}

/* ============================================================================
   1. SESSION STORAGE & AUTHENTICATED FASTAPI CLIENT (`/v1/*`)
   ============================================================================ */
function loadSavedSession() {
  try {
    const raw = localStorage.getItem(STORAGE_SESSION_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (parsed && parsed.accessToken && parsed.userId) return parsed;
  } catch (_) {}
  return null;
}

function saveSession(sess) {
  state.session = sess;
  if (sess) {
    localStorage.setItem(STORAGE_SESSION_KEY, JSON.stringify(sess));
  } else {
    localStorage.removeItem(STORAGE_SESSION_KEY);
    if (typeof signedUrlCache !== 'undefined') signedUrlCache.clear();
    if (typeof amTextureCache !== 'undefined') amTextureCache.clear();
  }
}

function getOrCreateGuestEmail() {
  let email = localStorage.getItem(STORAGE_GUEST_EMAIL_KEY);
  if (!email) {
    const rand = Math.random().toString(16).slice(2, 10);
    email = `lumina.${rand}@mindweave.app`;
    localStorage.setItem(STORAGE_GUEST_EMAIL_KEY, email);
  }
  return email;
}

let refreshPromise = null;
async function tryRefreshTokens() {
  if (!state.session || !state.session.refreshToken) return false;
  if (refreshPromise) return refreshPromise;
  refreshPromise = (async () => {
    try {
      const resp = await fetch(`${API_BASE}/auth/refresh`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refreshToken: state.session.refreshToken }),
      });
      if (!resp.ok) {
        saveSession(null);
        return false;
      }
      const data = await resp.json();
      saveSession({
        ...state.session,
        accessToken: data.accessToken,
        refreshToken: data.refreshToken,
      });
      return true;
    } catch (_) {
      return false;
    } finally {
      refreshPromise = null;
    }
  })();
  return refreshPromise;
}

async function apiFetch(path, options = {}, allowRetry = true) {
  const method = (options.method || 'GET').toUpperCase();
  const headers = new Headers(options.headers || {});
  const isLoginAuth =
    path === '/auth/email/otp:request' ||
    path === '/auth/refresh' ||
    (path === '/auth/email/otp:verify' && !options.attachBearer);

  if (!isLoginAuth && state.session && state.session.accessToken) {
    headers.set('Authorization', `Bearer ${state.session.accessToken}`);
  }
  if (['POST', 'PATCH', 'PUT', 'DELETE'].includes(method) && !path.startsWith('/auth/') && !headers.has('Idempotency-Key')) {
    headers.set('Idempotency-Key', makeUuid());
  }
  const isRawBody = options.body instanceof FormData || options.body instanceof Blob || typeof options.body === 'string';
  if (options.body && typeof options.body === 'object' && !isRawBody) {
    headers.set('Content-Type', 'application/json');
  }

  const resp = await fetch(`${API_BASE}${path}`, {
    ...options,
    method,
    headers,
    body: options.body && typeof options.body === 'object' && !isRawBody ? JSON.stringify(options.body) : options.body,
  });

  if (resp.status === 401 && !isLoginAuth && allowRetry) {
    const ok = await tryRefreshTokens();
    if (ok) return apiFetch(path, options, false);
    openAuthGate();
  }
  return resp;
}

async function apiJson(path, options = {}) {
  const resp = await apiFetch(path, options);
  if (resp.status === 204) return null;
  const text = await resp.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch (_) {
    data = null;
  }
  if (!resp.ok) {
    const err = new Error((data && (data.detail || data.title)) || `HTTP ${resp.status}`);
    err.status = resp.status;
    err.payload = data;
    throw err;
  }
  return data;
}

/* ============================================================================
   2. HYDRATION FROM `/v1/bootstrap` + `/v1/inbox`
   ============================================================================ */
const signedUrlCache = new Map(); // basePath -> { url, expSec }

function stabilizeSignedUrl(freshUrl, forceRefresh = false) {
  if (!freshUrl || typeof freshUrl !== 'string') return freshUrl;
  const qIdx = freshUrl.indexOf('?');
  if (qIdx === -1) return freshUrl;
  const basePath = freshUrl.slice(0, qIdx);
  const nowSec = Math.floor(Date.now() / 1000);
  if (!forceRefresh) {
    const cached = signedUrlCache.get(basePath);
    if (cached && cached.expSec - nowSec > 90) {
      return cached.url;
    }
  }
  const m = freshUrl.match(/[?&]exp=(\d+)/);
  const expSec = m ? Number(m[1]) : (nowSec + 600);
  signedUrlCache.set(basePath, { url: freshUrl, expSec });
  return freshUrl;
}

function mapBootstrapPerson(dto) {
  const vt = dto.visualTokens || {};
  const tier = dto.tier || 'gold';
  const records = (dto.materials || []).map((m, idx) => {
    const meta = m.cardMeta || {};
    const kind = m.type || m.kind || 'note';
    const rawBody = m.body || m.snippet || '';
    const imgUrl = stabilizeSignedUrl(m.mediaUrl || m.thumb || null);
    const dateLabel = meta.dateLabel || formatShortDate(m.happenedOn || m.createdAt);
    const segments =
      Array.isArray(meta.segments) && meta.segments.length
        ? meta.segments
        : rawBody
            .split(/(?<=[。！？!?])\s*/)
            .map((s) => s.trim())
            .filter(Boolean);
    const messages = Array.isArray(meta.messages) ? meta.messages : [];

    return {
      id: m.id,
      type: tier === 'silver' && kind !== 'photo' ? 'shield-log' : kind,
      rawKind: kind,
      date: dateLabel,
      time: meta.timeLabel || '',
      ring: meta.ring || ((idx % 3) + 1),
      angle: typeof meta.angle === 'number' ? meta.angle : ((idx * 58 + 38) % 340),
      tag: meta.tag || (kind === 'voice' ? '语音声囊' : kind === 'chat' ? '微信归档' : kind === 'photo' ? (m.title || '相片') : kind === 'draft' ? '待核对日期' : '手帐'),
      card_title: m.title || '',
      scene_label: meta.sceneLabel || m.title || '',
      caption: rawBody,
      text: rawBody,
      summary: meta.summary || m.snippet || '',
      segments: segments.length ? segments : [rawBody],
      duration: Number(meta.duration || Math.max(8, Math.min(45, Math.round(rawBody.length / 3)))),
      messages,
      palette: Array.isArray(meta.palette) && meta.palette.length >= 3 ? meta.palette : null,
      img: imgUrl,
      hasMedia: Boolean(imgUrl),
      lunar_hint: meta.lunarHint || '',
      lunar_label: meta.lunarLabel || '',
      fav: Boolean(m.isFavorite),
      version: m.version || 1,
      cardMeta: meta,
      happenedOn: m.happenedOn || null,
      createdAt: m.createdAt || null,
    };
  });

  const facts = (dto.facts || []).map((f) => ({
    id: f.id,
    type: f.type,
    kind: FACT_KIND_LABELS[f.type] || '小事',
    polarity: f.polarity || 'neutral',
    text: f.content,
    status: f.status,
    faded: f.status === 'rejected',
    version: f.version || 1,
    triggerDate: f.triggerDate || null,
    isLunar: Boolean(f.isLunar),
    lunarMonth: f.lunarMonth || null,
    lunarDay: f.lunarDay || null,
  }));

  const reminders = (dto.upcomingReminders || []).map((r) => {
    const dateStr = r.onceDate || (r.nextFireAt ? String(r.nextFireAt).slice(0, 10) : null);
    return {
      id: r.id,
      title: r.title,
      date_label: dateStr ? `${Number(dateStr.slice(5, 7))}月${Number(dateStr.slice(8, 10))}日` : '近期',
      lunar_label: r.isLunar && r.targetMonth && r.targetDay ? `农历${r.targetMonth}月${r.targetDay}日` : '公历提醒',
      lead: leadDaysLabel(r.leadDays),
      leadDays: r.leadDays || [1, 0],
      days_left: formatDaysLeft(dateStr),
      done: r.status === 'completed',
      status: r.status,
      version: r.version || 1,
      onceDate: dateStr,
    };
  });

  return {
    id: dto.id,
    name: dto.nameCn || '未命名',
    initial: dto.monogram || (dto.nameCn || '络').slice(0, 1),
    tier,
    shield: tier === 'silver',
    role: `${TIER_META[tier]?.label || '金箭'} · ${dto.roleTag || '手帐人物'}`,
    roleTag: dto.roleTag || '',
    contextSub: dto.contextSub || '',
    shortMemo: dto.shortMemo || '',
    traits: dto.traits || [],
    version: dto.version || 1,
    visualTokens: vt,
    mainPhotoUrl: tier === 'silver' ? null : stabilizeSignedUrl(dto.mainPhotoUrl || null),
    activeHeroMaterialId: vt.activeHeroMaterialId || null,
    coolingCount: dto.coolingCount || 0,
    facts,
    care_days: reminders,
    records,
    cupidPrompts: typeof vt.cupidPromptsCsv === 'string' && vt.cupidPromptsCsv.trim()
      ? vt.cupidPromptsCsv.split('|').filter(Boolean)
      : tier === 'silver'
        ? [`怎么稳妥地和「${dto.nameCn}」书面留痕对接？`, `帮我把刚才的边界记录理顺一下`]
        : tier === 'crystal'
          ? [`和「${dto.nameCn}」对接时要注意哪些小规矩？`, `帮我拟一段干脆礼貌的沟通回复`]
          : [`最近和「${dto.nameCn}」见面要注意哪些小细节？`, `结合手帐帮我准备一份贴心的小安排`],
  };
}

async function refreshBootstrap({ keepPersonId = true } = {}) {
  const prevId = keepPersonId ? state.personId : null;
  const [snap, inboxPage] = await Promise.all([
    apiJson('/bootstrap'),
    apiJson('/inbox?status=open&limit=30').catch(() => ({ items: [] })),
  ]);

  state.preferences = snap.preferences || { preferredEdition: 'lumina', version: 1 };
  state.inboxCount = snap.inboxCount || 0;
  state.inboxItems = (inboxPage && Array.isArray(inboxPage.items)) ? inboxPage.items : [];

  const orderedIds = [...(snap.coreIds || []), ...(snap.casualIds || [])];
  const seen = new Set();
  const list = [];
  for (const pid of orderedIds) {
    if (snap.people && snap.people[pid] && !seen.has(pid)) {
      seen.add(pid);
      list.push(mapBootstrapPerson(snap.people[pid]));
    }
  }
  Object.keys(snap.people || {}).forEach((pid) => {
    if (!seen.has(pid)) {
      seen.add(pid);
      list.push(mapBootstrapPerson(snap.people[pid]));
    }
  });
  state.people = list;

  // Prefer Lumina edition silently on first login if still default
  if (state.preferences && state.preferences.preferredEdition !== 'lumina' && state.preferences.version === 1) {
    apiJson('/me/preferences', {
      method: 'PATCH',
      headers: { 'If-Match': `"${state.preferences.version}"` },
      body: { preferredEdition: 'lumina' },
    })
      .then((updated) => {
        if (updated) state.preferences = updated;
      })
      .catch(() => {});
  }

  if (prevId && state.people.some((p) => p.id === prevId)) {
    state.personId = prevId;
  } else {
    state.personId = state.people.length ? state.people[0].id : null;
  }

  renderApp();
}

/* ============================================================================
   3. TRUE APPLE MUSIC MULTI-COPY RADIAL-TWIST ARTWORK ENGINE
   ============================================================================ */
const amTextureCache = new Map();

function rgbToHsl(r, g, b) {
  r /= 255; g /= 255; b /= 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
  let h = 0, s = 0;
  const l = (max + min) / 2;
  if (max !== min) {
    const d = max - min;
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    if (max === r) h = ((g - b) / d + (g < b ? 6 : 0)) / 6;
    else if (max === g) h = ((b - r) / d + 2) / 6;
    else h = ((r - g) / d + 4) / 6;
  }
  return [h * 360, s * 100, l * 100];
}

function hslToHex(h, s, l) {
  h = ((h % 360) + 360) % 360; s /= 100; l /= 100;
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
  const m = l - c / 2;
  let r = 0, g = 0, b = 0;
  if (h < 60) [r, g, b] = [c, x, 0];
  else if (h < 120) [r, g, b] = [x, c, 0];
  else if (h < 180) [r, g, b] = [0, c, x];
  else if (h < 240) [r, g, b] = [0, x, c];
  else if (h < 300) [r, g, b] = [x, 0, c];
  else [r, g, b] = [c, 0, x];
  return '#' + [r, g, b].map((v) => Math.round((v + m) * 255).toString(16).padStart(2, '0')).join('');
}

function analyzeRealPhotoZones(img) {
  const S = 48;
  const cv = document.createElement('canvas');
  cv.width = S; cv.height = S;
  const ctx = cv.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(img, 0, 0, S, S);
  const data = ctx.getImageData(0, 0, S, S).data;
  const tiles = [];
  const G = 4, step = S / G;
  let totalLuma = 0;
  let lumaCount = 0;
  for (let gy = 0; gy < G; gy++) {
    for (let gx = 0; gx < G; gx++) {
      let rSum = 0, gSum = 0, bSum = 0, cnt = 0;
      for (let y = Math.floor(gy * step); y < Math.floor((gy + 1) * step); y++) {
        for (let x = Math.floor(gx * step); x < Math.floor((gx + 1) * step); x++) {
          const idx = (y * S + x) * 4;
          rSum += data[idx]; gSum += data[idx + 1]; bSum += data[idx + 2]; cnt++;
        }
      }
      const r = Math.round(rSum / cnt), g = Math.round(gSum / cnt), b = Math.round(bSum / cnt);
      const [h, s, l] = rgbToHsl(r, g, b);
      const warmth = (r - b) / 255 + (g - b) * 0.25 / 255;
      const vividness = s * (1 - Math.abs(l - 50) / 56);
      const luma = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
      totalLuma += luma;
      lumaCount++;
      tiles.push({ cx: (gx + 0.5) / G, cy: (gy + 0.5) / G, r, g, b, h, s, l, warmth, vividness, luma });
    }
  }
  const avgLuma = lumaCount ? totalLuma / lumaCount : 0.35;
  const byWarm = [...tiles].sort((a, b) => (b.warmth * 0.7 + b.vividness / 140) - (a.warmth * 0.7 + a.vividness / 140));
  const byDeep = [...tiles].sort((a, b) => (a.warmth * 0.6 + a.l / 120) - (b.warmth * 0.6 + b.l / 120));
  const byAiry = [...tiles].sort((a, b) => (b.l - b.s * 0.15) - (a.l - a.s * 0.15));
  const byVivid = [...tiles].sort((a, b) => b.vividness - a.vividness);

  const warmTile = byWarm[0];
  const deepTile = byDeep.find((t) => Math.hypot(t.cx - warmTile.cx, t.cy - warmTile.cy) > 0.28) || byDeep[0];
  const airyTile = byAiry[0];
  const vividTile = byVivid[0];
  const liftRealHex = (tile, targetL, minS = 14) => hslToHex(tile.h, Math.min(72, Math.max(minS, tile.s * 1.05)), targetL);

  return {
    warmTile, deepTile, airyTile, vividTile,
    avgLuma,
    isBright: avgLuma > 0.54,
    realWarm: liftRealHex(warmTile, 72, 22),
    realCool: liftRealHex(deepTile, 70, 14),
    realLight: liftRealHex(airyTile, 84, 12),
    realVivid: liftRealHex(vividTile, 68, 24),
    deepHex: hslToHex(deepTile.h, Math.min(36, deepTile.s * 0.75), 11),
  };
}

/**
 * Engine-independent pure-JS separable 2-pass box blur + saturation/brightness/contrast pass.
 * Guarantees 100% silky Apple Music liquid glass on Apple WebKit & iOS WKWebView where
 * CanvasRenderingContext2D.filter is unsupported or silently ignored.
 */
function blurAndGradeCanvas(canvas, radius, satVal, brightVal, contrastVal = 1.05) {
  const S = canvas.width;
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  const img = ctx.getImageData(0, 0, S, S);
  const src = img.data;
  const tmp = new Uint8ClampedArray(src.length);
  const dst = new Uint8ClampedArray(src.length);
  const r = Math.max(2, Math.min(22, Math.round(radius)));
  const span = r * 2 + 1;

  const boxPass = (inArr, outArr) => {
    // Horizontal pass (clamp-to-edge)
    for (let y = 0; y < S; y++) {
      let rAcc = 0, gAcc = 0, bAcc = 0;
      const rowOff = y * S;
      for (let dx = -r; dx <= r; dx++) {
        const ix = Math.max(0, Math.min(S - 1, dx));
        const p = (rowOff + ix) * 4;
        rAcc += inArr[p]; gAcc += inArr[p + 1]; bAcc += inArr[p + 2];
      }
      for (let x = 0; x < S; x++) {
        const op = (rowOff + x) * 4;
        tmp[op] = rAcc / span;
        tmp[op + 1] = gAcc / span;
        tmp[op + 2] = bAcc / span;
        tmp[op + 3] = 255;
        const xRem = Math.max(0, x - r);
        const xAdd = Math.min(S - 1, x + r + 1);
        const pRem = (rowOff + xRem) * 4;
        const pAdd = (rowOff + xAdd) * 4;
        rAcc += inArr[pAdd] - inArr[pRem];
        gAcc += inArr[pAdd + 1] - inArr[pRem + 1];
        bAcc += inArr[pAdd + 2] - inArr[pRem + 2];
      }
    }
    // Vertical pass (clamp-to-edge)
    for (let x = 0; x < S; x++) {
      let rAcc = 0, gAcc = 0, bAcc = 0;
      for (let dy = -r; dy <= r; dy++) {
        const iy = Math.max(0, Math.min(S - 1, dy));
        const p = (iy * S + x) * 4;
        rAcc += tmp[p]; gAcc += tmp[p + 1]; bAcc += tmp[p + 2];
      }
      for (let y = 0; y < S; y++) {
        const op = (y * S + x) * 4;
        outArr[op] = rAcc / span;
        outArr[op + 1] = gAcc / span;
        outArr[op + 2] = bAcc / span;
        outArr[op + 3] = 255;
        const yRem = Math.max(0, y - r);
        const yAdd = Math.min(S - 1, y + r + 1);
        const pRem = (yRem * S + x) * 4;
        const pAdd = (yAdd * S + x) * 4;
        rAcc += tmp[pAdd] - tmp[pRem];
        gAcc += tmp[pAdd + 1] - tmp[pRem + 1];
        bAcc += tmp[pAdd + 2] - tmp[pRem + 2];
      }
    }
  };

  // Two cascaded box passes approximate a smooth Gaussian blur cleanly in ~1.5ms on 128x128!
  boxPass(src, dst);
  boxPass(dst, src);

  // Saturation + brightness + gentle contrast grade
  for (let i = 0; i < src.length; i += 4) {
    let rr = src[i], gg = src[i + 1], bb = src[i + 2];
    const lum = 0.2126 * rr + 0.7152 * gg + 0.0722 * bb;
    rr = lum + (rr - lum) * satVal;
    gg = lum + (gg - lum) * satVal;
    bb = lum + (bb - lum) * satVal;
    rr = ((rr * brightVal - 128) * contrastVal) + 128;
    gg = ((gg * brightVal - 128) * contrastVal) + 128;
    bb = ((bb * brightVal - 128) * contrastVal) + 128;
    src[i] = Math.max(0, Math.min(255, rr));
    src[i + 1] = Math.max(0, Math.min(255, gg));
    src[i + 2] = Math.max(0, Math.min(255, bb));
    src[i + 3] = 255;
  }
  ctx.putImageData(img, 0, 0);
}

function renderTwistedArtworkLayer(srcCanvas, S, focusX, focusY, zoom, twistA, twistB, satVal, brightVal, blurPx) {
  const cropCv = document.createElement('canvas');
  cropCv.width = S; cropCv.height = S;
  const cctx = cropCv.getContext('2d', { willReadFrequently: true });
  const srcW = srcCanvas.width, srcH = srcCanvas.height;
  const cropW = srcW / zoom, cropH = srcH / zoom;
  const sx = Math.max(0, Math.min(srcW - cropW, focusX * srcW - cropW * 0.5));
  const sy = Math.max(0, Math.min(srcH - cropH, focusY * srcH - cropH * 0.5));
  cctx.drawImage(srcCanvas, sx, sy, cropW, cropH, 0, 0, S, S);

  const srcImg = cctx.getImageData(0, 0, S, S);
  const dstImg = cctx.createImageData(S, S);
  const sd = srcImg.data, dd = dstImg.data;
  const cx1 = S * 0.36, cy1 = S * 0.38, r1 = S * 0.72;
  const cx2 = S * 0.68, cy2 = S * 0.66, r2 = S * 0.68;

  for (let y = 0; y < S; y++) {
    for (let x = 0; x < S; x++) {
      let u = x, v = y;
      let dx = u - cx1, dy = v - cy1, dist = Math.hypot(dx, dy);
      if (dist < r1) {
        const rat = (r1 - dist) / r1, th = rat * rat * twistA;
        const co = Math.cos(th), si = Math.sin(th);
        u = cx1 + dx * co - dy * si; v = cy1 + dx * si + dy * co;
      }
      dx = u - cx2; dy = v - cy2; dist = Math.hypot(dx, dy);
      if (dist < r2) {
        const rat = (r2 - dist) / r2, th = rat * rat * twistB;
        const co = Math.cos(th), si = Math.sin(th);
        u = cx2 + dx * co - dy * si; v = cy2 + dx * si + dy * co;
      }
      const ix = Math.max(0, Math.min(S - 1, Math.round(u)));
      const iy = Math.max(0, Math.min(S - 1, Math.round(v)));
      const si4 = (iy * S + ix) * 4, di4 = (y * S + x) * 4;
      dd[di4] = sd[si4]; dd[di4 + 1] = sd[si4 + 1]; dd[di4 + 2] = sd[si4 + 2]; dd[di4 + 3] = 255;
    }
  }
  cctx.putImageData(dstImg, 0, 0);

  const out = document.createElement('canvas');
  out.width = S; out.height = S;
  const octx = out.getContext('2d', { willReadFrequently: true });
  octx.drawImage(cropCv, -S * 0.22, -S * 0.22, S * 1.44, S * 1.44);
  octx.save(); octx.globalAlpha = 0.50; octx.translate(S * 0.52, S * 0.48); octx.rotate(1.95);
  octx.drawImage(cropCv, -S * 0.78, -S * 0.78, S * 1.56, S * 1.56); octx.restore();
  octx.save(); octx.globalAlpha = 0.34; octx.translate(S * 0.46, S * 0.54); octx.rotate(-1.18);
  octx.drawImage(cropCv, -S * 0.76, -S * 0.76, S * 1.52, S * 1.52); octx.restore();

  // Engine-independent Gaussian-like blur + liquid color grading (100% WebKit/iOS native safe!)
  blurAndGradeCanvas(out, blurPx + 3, satVal, brightVal, 1.06);
  return out.toDataURL('image/jpeg', 0.84);
}

const photoCacheGen = Object.create(null);

function bumpPhotoCacheGen(personId) {
  if (!personId) return;
  photoCacheGen[personId] = (photoCacheGen[personId] || 0) + 1;
}

function stablePhotoCacheKey(personId, photoUrl) {
  const base = String(photoUrl || '').split('?')[0];
  return `${base}#g${photoCacheGen[personId] || 0}`;
}

function bakeAppleMusicTextures(img, zones) {
  const S = 96;
  const srcCv = document.createElement('canvas');
  srcCv.width = S; srcCv.height = S;
  const sctx = srcCv.getContext('2d');
  const minDim = Math.min(img.naturalWidth || img.width, img.naturalHeight || img.height);
  const ox = ((img.naturalWidth || img.width) - minDim) * 0.5;
  const oy = ((img.naturalHeight || img.height) - minDim) * 0.5;
  sctx.drawImage(img, ox, oy, minDim, minDim, 0, 0, S, S);
  const adaptiveSat = (tile, targetBoost) => +(targetBoost - Math.min(1, Math.max(0, (tile.s || 30) / 85)) * (targetBoost - 1.12)).toFixed(2);
  const safeZoom = (tile, z) => (tile.s > 55 ? Math.min(z, 1.12) : z);
  return {
    full:  renderTwistedArtworkLayer(srcCv, S, 0.50, 0.50, 1.06,  2.2, -1.9, adaptiveSat(zones.vividTile, 1.50), 0.96, 11),
    warm:  renderTwistedArtworkLayer(srcCv, S, zones.warmTile.cx,  zones.warmTile.cy,  safeZoom(zones.warmTile, 1.24),  2.5, -1.7, adaptiveSat(zones.warmTile, 1.54), 0.99, 10),
    deep:  renderTwistedArtworkLayer(srcCv, S, zones.deepTile.cx,  zones.deepTile.cy,  safeZoom(zones.deepTile, 1.28), -2.3,  2.0, adaptiveSat(zones.deepTile, 1.38), 0.88, 10),
    airy:  renderTwistedArtworkLayer(srcCv, S, zones.airyTile.cx,  zones.airyTile.cy,  safeZoom(zones.airyTile, 1.24),  1.9, -2.2, adaptiveSat(zones.airyTile, 1.36), 1.04, 11),
    vivid: renderTwistedArtworkLayer(srcCv, S, zones.vividTile.cx, zones.vividTile.cy, safeZoom(zones.vividTile, 1.26), -2.6,  1.8, adaptiveSat(zones.vividTile, 1.56), 0.96, 10),
  };
}

function applyChromaTokens(t) {
  syncOrbPaletteCache(t);
  const r = document.documentElement.style;
  document.documentElement.setAttribute('data-photo-luma', t.isBright ? 'bright' : 'normal');
  r.setProperty('--photo-deep', t.deepHex || '#1E241C');
  r.setProperty('--real-warm', t.realWarm || '#E5BC8B');
  r.setProperty('--real-cool', t.realCool || '#88A89A');
  r.setProperty('--real-light', t.realLight || '#F2E7D8');
  r.setProperty('--real-vivid', t.realVivid || '#C97D5E');
  if (t.textures) {
    ['full', 'warm', 'deep', 'airy', 'vivid'].forEach((k) => r.setProperty(`--am-art-${k}`, `url("${t.textures[k]}")`));
  } else {
    ['full', 'warm', 'deep', 'airy', 'vivid'].forEach((k) => r.setProperty(`--am-art-${k}`, 'none'));
  }
}

function syncChromaForPerson(p) {
  if (!p) {
    document.documentElement.setAttribute('data-tier', 'crystal');
    document.documentElement.setAttribute('data-has-photo', 'false');
    document.documentElement.setAttribute('data-face-zone', 'none');
    document.documentElement.setAttribute('data-photo-luma', 'normal');
    applyChromaTokens({ deepHex: '#243445', realWarm: '#6F9BC2', realCool: '#5B84A6', realLight: '#E6F1FA', realVivid: '#4E7EA6', textures: null, isBright: false });
    return;
  }

  const isSilver = p.tier === 'silver';
  const photoUrl = !isSilver && p.mainPhotoUrl ? p.mainPhotoUrl : null;
  const photoKey = photoUrl ? stablePhotoCacheKey(p.id, photoUrl) : null;
  const vt = p.visualTokens || {};
  document.documentElement.setAttribute('data-tier', p.tier);
  document.documentElement.setAttribute('data-has-photo', photoUrl ? 'true' : 'false');

  const bgImg = document.getElementById('backdropImg');
  if (photoUrl && bgImg) {
    if (bgImg.getAttribute('data-loaded-key') !== photoKey) {
      bgImg.src = photoUrl;
      bgImg.setAttribute('data-loaded-key', photoKey);
    }
    const faceZone = vt.faceZone || 'center';
    document.documentElement.setAttribute('data-face-zone', faceZone);
    const isNarrow = document.documentElement.classList.contains('v15-iphone-sim') || document.documentElement.classList.contains('is-native-ios') || window.innerWidth <= 768;
    bgImg.style.objectPosition = isNarrow ? (vt.phoneFocus || '54% 24%') : (vt.deskFocus || '54% 24%');
  } else {
    document.documentElement.setAttribute('data-face-zone', 'none');
    document.documentElement.setAttribute('data-photo-luma', 'normal');
    if (bgImg) {
      bgImg.removeAttribute('src');
      bgImg.removeAttribute('data-loaded-key');
    }
  }

  if (!photoUrl || !photoKey) {
    applyChromaTokens(
      isSilver
        ? { deepHex: '#1E2C3A', realWarm: '#56728C', realCool: '#476078', realLight: '#DCE7F2', realVivid: '#3E5A75', textures: null, isBright: false }
        : { deepHex: '#243445', realWarm: '#6F9BC2', realCool: '#5B84A6', realLight: '#E6F1FA', realVivid: '#4E7EA6', textures: null, isBright: false },
    );
    return;
  }

  // Apply server-extracted real-photo tokens immediately for zero-flash paint
  if (vt.realWarm) {
    const cached = amTextureCache.get(photoKey);
    applyChromaTokens({
      deepHex: vt.deepHex || '#1E241C',
      realWarm: vt.realWarm,
      realCool: vt.realCool || '#88A89A',
      realLight: vt.realLight || '#F2E7D8',
      realVivid: vt.realVivid || '#C97D5E',
      textures: cached?.textures || null,
      isBright: Boolean(cached?.isBright),
    });
  }

  if (amTextureCache.has(photoKey)) {
    applyChromaTokens(amTextureCache.get(photoKey));
    return;
  }

  const probe = new Image();
  probe.crossOrigin = 'anonymous';
  probe.onload = () => {
    try {
      const zones = analyzeRealPhotoZones(probe);
      const textures = bakeAppleMusicTextures(probe, zones);
      const res = { ...zones, textures };
      amTextureCache.set(photoKey, res);
      if (currentPerson()?.id === p.id) applyChromaTokens(res);
    } catch (_) {}
  };
  probe.src = photoUrl;
}

function amMeshHtml(phaseSec = 0) {
  const p = currentPerson();
  if (!p || p.tier === 'silver' || !p.mainPhotoUrl) return '';
  const degA = Math.round(((Math.abs(phaseSec) * 37) % 360));
  const degB = Math.round(((Math.abs(phaseSec) * 53 + 145) % 360));
  return `<div class="am-card-canvas" style="--am-deg-a:${degA}deg;--am-deg-b:${degB}deg" aria-hidden="true"><div class="am-art-layer am-art-base"></div><div class="am-art-layer am-art-eddy"></div><div class="am-art-veil"></div></div>`;
}

/* ============================================================================
   4. ANIMATED PHOTO-REACTIVE FLUID CUPID ORB
   ============================================================================ */
let orbT = 0;
let orbSpeed = 1.0;
let lastOrbTs = 0;
const orbPalette = {
  cLight: '#F4E5CE',
  cWarm: '#DFAA74',
  cVivid: '#C46E50',
  cDeep: '#222A24',
};

function syncOrbPaletteCache(t) {
  orbPalette.cLight = t.realLight || '#F4E5CE';
  orbPalette.cWarm = t.realWarm || '#DFAA74';
  orbPalette.cVivid = t.realVivid || '#C46E50';
  orbPalette.cDeep = t.deepHex || '#222A24';
}

function renderFluidOrbToCanvas(canvas, size) {
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  if (canvas.width !== size * dpr) { canvas.width = size * dpr; canvas.height = size * dpr; }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const p = currentPerson();
  const isSilver = p && p.tier === 'silver';
  const isCrystal = !p || (p.tier === 'crystal' && !p.mainPhotoUrl);
  const cLight = isSilver ? '#EBF3FA' : isCrystal ? '#EAF4FC' : orbPalette.cLight;
  const cWarm  = isSilver ? '#8BA5BD' : isCrystal ? '#79A9CF' : orbPalette.cWarm;
  const cVivid = isSilver ? '#496783' : isCrystal ? '#5A88B4' : orbPalette.cVivid;
  const cDeep  = isSilver ? '#1D2C3C' : isCrystal ? '#243A50' : orbPalette.cDeep;

  ctx.clearRect(0, 0, size, size);
  ctx.save();
  ctx.beginPath(); ctx.arc(size / 2, size / 2, size / 2, 0, Math.PI * 2); ctx.clip();

  const baseG = ctx.createLinearGradient(0, 0, size, size);
  baseG.addColorStop(0, cDeep); baseG.addColorStop(0.58, cVivid); baseG.addColorStop(1, cDeep);
  ctx.fillStyle = baseG; ctx.fillRect(0, 0, size, size);

  const blobs = [
    { x: size * (0.5 + 0.25 * Math.cos(orbT * 1.1)), y: size * (0.5 + 0.23 * Math.sin(orbT * 0.9)), r: size * 0.44, c: cWarm },
    { x: size * (0.5 + 0.27 * Math.sin(orbT * 0.8 + 2.1)), y: size * (0.5 + 0.25 * Math.cos(orbT * 1.3)), r: size * 0.36, c: cLight },
    { x: size * (0.5 + 0.22 * Math.cos(-orbT * 1.2 + 4.0)), y: size * (0.5 + 0.26 * Math.sin(-orbT * 0.7)), r: size * 0.40, c: cVivid },
  ];
  blobs.forEach((b) => {
    const g = ctx.createRadialGradient(b.x, b.y, 1, b.x, b.y, b.r);
    g.addColorStop(0, b.c); g.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.globalCompositeOperation = 'screen';
    ctx.fillStyle = g;
    ctx.beginPath(); ctx.arc(b.x, b.y, b.r, 0, Math.PI * 2); ctx.fill();
  });

  ctx.globalCompositeOperation = 'source-over';
  const rim = ctx.createRadialGradient(size * 0.5, size * 0.5, size * 0.24, size * 0.5, size * 0.5, size * 0.52);
  rim.addColorStop(0, 'rgba(0,0,0,0)'); rim.addColorStop(0.75, 'rgba(8,10,14,0.16)'); rim.addColorStop(1, 'rgba(4,6,10,0.52)');
  ctx.fillStyle = rim; ctx.fillRect(0, 0, size, size);

  const spec = ctx.createRadialGradient(size * 0.30, size * 0.24, 1, size * 0.30, size * 0.24, size * 0.38);
  spec.addColorStop(0, 'rgba(255,255,255,0.72)'); spec.addColorStop(0.45, 'rgba(255,255,255,0.16)'); spec.addColorStop(1, 'rgba(255,255,255,0)');
  ctx.fillStyle = spec; ctx.fillRect(0, 0, size, size);
  ctx.restore();
}

function animateOrbs(ts = 0) {
  if (!lastOrbTs || ts - lastOrbTs >= 42) {
    lastOrbTs = ts;
    const target = state.cupidThinking ? 3.4 : 1.0;
    orbSpeed += (target - orbSpeed) * 0.14;
    orbT += 0.045 * orbSpeed;
    renderFluidOrbToCanvas(document.getElementById('dockCupidCanvas'), 42);
    if (document.getElementById('cupidFullscreen')?.classList.contains('is-open')) {
      renderFluidOrbToCanvas(document.getElementById('cupidHeadCanvas'), 32);
    }
  }
  requestAnimationFrame(animateOrbs);
}

/* ============================================================================
   5. MAIN RENDER: EMPTY ONBOARDING vs. JOURNAL STAGE & 5-VESSEL FEED
   ============================================================================ */
function renderApp() {
  const p = currentPerson();
  const emptyStage = document.getElementById('emptyWelcomeStage');
  const portraitWin = document.getElementById('portraitWindow');
  const feedVp = document.getElementById('feedViewport');
  const dock = document.getElementById('glebTriDock');
  const pinWrap = document.getElementById('arrowPinWrap');

  if (!p) {
    emptyStage.classList.add('is-show');
    portraitWin.style.display = 'none';
    feedVp.innerHTML = '';
    dock.style.display = 'none';
    pinWrap.style.visibility = 'hidden';
    syncChromaForPerson(null);
    return;
  }

  emptyStage.classList.remove('is-show');
  portraitWin.style.display = '';
  dock.style.display = '';
  pinWrap.style.visibility = 'visible';

  syncChromaForPerson(p);
  renderTriDockSeals();
  renderFeed();
}

function renderTopAndMasthead() {
  const p = currentPerson();
  if (!p) return;
  document.documentElement.setAttribute('data-scale', state.scale);
  document.getElementById('personNameTitle').textContent = p.name;
  document.getElementById('arrowPinImg').src = ARROW_PINS[p.tier] || ARROW_PINS.gold;
  document.getElementById('arrowPinLabel').textContent = TIER_META[p.tier]?.label || '金箭';
  document.querySelectorAll('[data-tier-pick]').forEach((b) => b.classList.toggle('is-active', b.dataset.tierPick === p.tier));
  document.querySelectorAll('.scale-tab').forEach((t) => t.classList.toggle('is-active', t.dataset.scale === state.scale));

  const rightBtn = document.getElementById('btnScaleRightAction');
  if (p.tier === 'silver') {
    rightBtn.textContent = state.shieldGlobalOpen ? '收起留痕' : '展开全部留痕';
  } else {
    rightBtn.textContent = `2026 · ${p.records.length} 页`;
  }

  const firstCare = (p.care_days && p.care_days[0]) || null;
  const ribTxt = document.getElementById('careRibbonText');
  const ribEta = document.getElementById('careRibbonEta');
  if (firstCare) {
    ribTxt.textContent = `${firstCare.date_label} · ${firstCare.title}`;
    ribEta.textContent = firstCare.done ? '✓ 已记完' : `${firstCare.days_left} ›`;
  } else if (p.shortMemo) {
    ribTxt.textContent = p.shortMemo.slice(0, 42);
    ribEta.textContent = '档案 ›';
  } else {
    ribTxt.textContent = `轻点打开「${p.name}」人物档案与记挂日程`;
    ribEta.textContent = '查看 ›';
  }
}

function renderTriDockSeals() {
  const strip = document.getElementById('dockSealsCluster');
  const featured = state.people.slice(0, 5);
  if (state.personId && !featured.some((x) => x.id === state.personId)) {
    const active = currentPerson();
    if (active) featured[Math.min(4, featured.length - 1)] = active;
  }
  strip.innerHTML = featured.map((p) => {
    const isSilver = p.tier === 'silver';
    const isCrystal = p.tier === 'crystal';
    const imgUrl = !isSilver && p.mainPhotoUrl ? p.mainPhotoUrl : null;
    const cls = [
      'dock-seal-btn',
      p.id === state.personId ? 'is-active' : '',
      isSilver ? 'is-silver-seal' : '',
      isCrystal && !imgUrl ? 'is-crystal-seal' : '',
    ].filter(Boolean).join(' ');
    return `<button class="${cls}" data-dock-pid="${p.id}" type="button" title="${escapeHtml(p.name)} · ${TIER_META[p.tier]?.label || ''}">
      ${imgUrl ? `<img src="${imgUrl}" alt="${escapeHtml(p.name)}" />` : escapeHtml(p.initial || p.name.slice(0, 1))}
    </button>`;
  }).join('');
  strip.querySelectorAll('[data-dock-pid]').forEach((b) => {
    b.addEventListener('click', () => selectPerson(b.dataset.dockPid));
  });
}

function buildVoiceBarsHtml(rec) {
  const heights = [36, 62, 48, 86, 70, 42, 94, 58, 76, 44, 88, 64, 50, 92, 68, 40, 82, 54, 72, 46, 84, 56, 38];
  const litCnt = state.playingVoiceId === rec.id ? Math.floor(state.voiceProgress * heights.length) : 0;
  return heights.map((h, i) => `<i class="${i < litCnt ? 'is-lit' : ''}" style="height:${h}%"></i>`).join('');
}

function renderFolioFeed(vp, p) {
  const isSilver = p.tier === 'silver';
  const hasPhoto = !isSilver && Boolean(p.mainPhotoUrl);
  vp.classList.toggle('is-folio-wings', hasPhoto);

  const facts = (p.facts || []).filter((f) => !f.faded);
  const recs = [...p.records];
  const items = [];

  if (!isSilver) {
    const firstVoiceIdx = recs.findIndex((r) => r.type === 'voice');
    const firstPhotoIdx = recs.findIndex((r) => r.type === 'photo' && r.img);
    const firstChatIdx  = recs.findIndex((r) => r.type === 'chat');
    const used = new Set();

    if (facts[0]) items.push({ _kind: 'fact', data: facts[0], id: facts[0].id });
    if (firstVoiceIdx >= 0) { items.push({ _kind: 'rec', data: recs[firstVoiceIdx] }); used.add(firstVoiceIdx); }
    if (firstPhotoIdx >= 0) { items.push({ _kind: 'rec', data: recs[firstPhotoIdx] }); used.add(firstPhotoIdx); }
    if (facts[1]) items.push({ _kind: 'fact', data: facts[1], id: facts[1].id });
    if (firstChatIdx >= 0)  { items.push({ _kind: 'rec', data: recs[firstChatIdx] }); used.add(firstChatIdx); }
    if (facts[2]) items.push({ _kind: 'fact', data: facts[2], id: facts[2].id });
    recs.forEach((r, idx) => { if (!used.has(idx)) items.push({ _kind: 'rec', data: r }); });
  } else {
    recs.forEach((r) => items.push({ _kind: 'rec', data: r }));
  }

  const renderItemCard = (item, idx) => {
    const staggerCls = idx % 2 === 0 ? 'is-stagger-left' : 'is-stagger-right';
    const phase = -(idx * 5 + 1);

    if (item._kind === 'fact') {
      const f = item.data;
      return `
        <article class="slip-shell kind-fact ${staggerCls}" data-open-dossier-fact="1" style="cursor:pointer;">
          <div class="slip-inner">
            ${amMeshHtml(phase)}
            <div class="slip-body">
              <div class="fact-pill-kicker">
                <span class="fact-pill-dot" aria-hidden="true"></span>
                <span>${escapeHtml(f.kind)}</span>
              </div>
              <div class="fact-pill-text">${escapeHtml(f.text)}</div>
            </div>
          </div>
        </article>
      `;
    }

    const rec = item.data;
    if (isSilver || rec.type === 'shield-log') {
      const veiled = !(state.shieldGlobalOpen || state.unveiledIds.has(rec.id));
      const bodyTxt = rec.text || rec.summary || (rec.segments && rec.segments.join('')) || rec.caption || '';
      return `
        <article class="slip-shell kind-silver" data-rec-id="${rec.id}">
          <div class="slip-inner">
            <div class="slip-body">
              <div class="fluted-veil-box ${veiled ? 'is-veiled' : ''}">
                <div class="fluted-raw-text">${escapeHtml(bodyTxt)}</div>
                <div class="fluted-glass-overlay">
                  <span style="font-size:12px;color:#243445;font-weight:500;">边界留痕已收起 · ${escapeHtml(rec.date)}</span>
                  <button class="fluted-unveil-btn" data-unveil-id="${rec.id}" type="button">查看原文</button>
                </div>
              </div>
              <div class="slip-foot">
                <button class="slip-fav-btn ${rec.fav ? 'is-fav' : ''}" data-fav-id="${rec.id}" type="button">${rec.fav ? '★' : '☆'}</button>
                <span style="font-family:var(--font-mono);">${escapeHtml(rec.date)}</span>
              </div>
            </div>
          </div>
        </article>
      `;
    }

    if (rec.type === 'photo' && rec.img) {
      const pal = (rec.palette || ['#D8C2A0', '#8B7D6B', '#463D34']).slice(0, 3);
      const isCurrentBg = p.activeHeroMaterialId === rec.id;
      return `
        <article class="slip-shell kind-photo ${staggerCls}" data-rec-id="${rec.id}">
          <div class="slip-inner">
            <div class="photo-slip-stage" data-open-lb="${rec.id}">
              <img src="${rec.img}" alt="${escapeHtml(rec.caption)}" />
              <div class="photo-slip-scrim"></div>
              <div class="photo-top-bar">
                <span class="photo-date-chip">${escapeHtml(rec.scene_label || rec.tag)}</span>
                <span class="photo-pigments" style="position:static;" aria-hidden="true">${pal.map((c) => `<i style="background:${c}"></i>`).join('')}</span>
              </div>
              <div class="photo-slip-caption-wrap">
                <div class="photo-slip-caption">${escapeHtml(rec.caption)}</div>
                <div class="photo-foot-actions">
                  <button class="photo-hero-pill" data-quick-bg-mat="${rec.id}" type="button">${isCurrentBg ? '✓ 当前背景' : '✦ 映作背景'}</button>
                  <button class="slip-fav-btn ${rec.fav ? 'is-fav' : ''}" data-fav-id="${rec.id}" type="button" style="color:#FAF6F0;">${rec.fav ? '★' : '☆'}</button>
                  <span style="font-family:var(--font-mono);font-size:11px;opacity:0.84;">${escapeHtml(rec.date)}</span>
                </div>
              </div>
            </div>
          </div>
        </article>
      `;
    }

    if (rec.type === 'voice') {
      const playing = state.playingVoiceId === rec.id;
      const segs = rec.segments || [rec.text || ''];
      const activeSegIdx = playing ? Math.min(segs.length - 1, Math.floor(state.voiceProgress * segs.length)) : -1;
      return `
        <article class="slip-shell kind-voice ${staggerCls}" data-rec-id="${rec.id}">
          <div class="slip-inner">
            ${amMeshHtml(phase)}
            <div class="slip-body">
              <div class="chat-head-row">
                <span class="chat-tag-pill">● ${escapeHtml(rec.date)} · 语音声囊</span>
                <button class="slip-fav-btn ${rec.fav ? 'is-fav' : ''}" data-fav-id="${rec.id}" type="button">${rec.fav ? '★' : '☆'}</button>
              </div>
              <div class="voice-transcript">
                ${segs.map((sg, si) => `<span class="voice-seg ${si === activeSegIdx ? 'is-spoken' : ''}">${escapeHtml(sg)}</span>`).join('')}
              </div>
              <div class="voice-bar-row">
                <button class="voice-play-orb" data-play-voice="${rec.id}" type="button" aria-label="${playing ? '暂停' : '播放'}">${playing ? '❚❚' : '▶'}</button>
                <div class="voice-bars" aria-hidden="true">${buildVoiceBarsHtml(rec)}</div>
                <span class="voice-dur">0:${String(rec.duration || 16).padStart(2, '0')}</span>
              </div>
            </div>
          </div>
        </article>
      `;
    }

    if (rec.type === 'chat') {
      const msgs = (rec.messages || []).slice(0, 4);
      return `
        <article class="slip-shell kind-chat ${staggerCls}" data-rec-id="${rec.id}">
          <div class="slip-inner">
            ${amMeshHtml(phase)}
            <div class="slip-body">
              <div class="chat-head-row">
                <span class="chat-tag-pill">● ${escapeHtml(rec.date)} · 微信归档</span>
                <button class="slip-fav-btn ${rec.fav ? 'is-fav' : ''}" data-fav-id="${rec.id}" type="button">${rec.fav ? '★' : '☆'}</button>
              </div>
              <div style="font-family:var(--font-serif);font-size:15px;font-weight:600;letter-spacing:0.02em;">${escapeHtml(rec.card_title || rec.tag)}</div>
              ${rec.summary ? `<div class="chat-takeaway">${escapeHtml(rec.summary)}</div>` : ''}
              ${msgs.length ? `
                <div class="chat-exchange">
                  ${msgs.map((m) => {
                    const isMe = m.from === '我' || m.sender === 'me' || m.is_target === false;
                    const spk = isMe ? '我' : (m.from || m.sender || p.name);
                    return `<div class="chat-msg-row ${isMe ? 'is-me' : ''}"><span class="chat-speaker">${escapeHtml(spk)}</span><span>${escapeHtml(m.text)}</span></div>`;
                  }).join('')}
                </div>
              ` : `<div class="voice-transcript">${escapeHtml(rec.text)}</div>`}
            </div>
          </div>
        </article>
      `;
    }

    if (rec.type === 'draft') {
      return `
        <article class="slip-shell kind-draft ${staggerCls}" data-rec-id="${rec.id}">
          <div class="slip-inner">
            ${amMeshHtml(phase)}
            <div class="slip-body">
              <div class="voice-transcript">${escapeHtml(rec.text)}</div>
              <button class="draft-pencil-hint" data-open-wheel="${rec.id}" type="button">
                <span>${escapeHtml(rec.lunar_hint || '12月16日 ✎ · 轻点核对')}</span>
              </button>
              <div class="slip-foot">
                <button class="slip-fav-btn ${rec.fav ? 'is-fav' : ''}" data-fav-id="${rec.id}" type="button">${rec.fav ? '★' : '☆'}</button>
                <span style="font-family:var(--font-mono);">${escapeHtml(rec.date)}</span>
              </div>
            </div>
          </div>
        </article>
      `;
    }

    return `
      <article class="slip-shell kind-note ${staggerCls}" data-rec-id="${rec.id}">
        <div class="slip-inner">
          ${amMeshHtml(phase)}
          <div class="slip-body">
            ${rec.card_title ? `<div style="font-family:var(--font-serif);font-size:15px;font-weight:600;">${escapeHtml(rec.card_title)}</div>` : ''}
            <div class="voice-transcript">${escapeHtml(rec.text || rec.summary || '')}</div>
            <div class="slip-foot">
              <button class="slip-fav-btn ${rec.fav ? 'is-fav' : ''}" data-fav-id="${rec.id}" type="button">${rec.fav ? '★' : '☆'}</button>
              <span style="font-family:var(--font-mono);">${escapeHtml(rec.date)}</span>
            </div>
          </div>
        </div>
      </article>
    `;
  };

  const pendingMention = state.inboxItems.find((it) => it.kind === 'entity_disambiguation' || it.kind === 'new_person');
  const whisperHtml = !isSilver && pendingMention
    ? `<button class="question-whisper-btn" id="btnQuestionEntry" type="button">
         <span>● 发现待确认人物「${escapeHtml(pendingMention.mention || '新人物')}」· 轻点归属</span>
         <span>›</span>
       </button>`
    : '';

  if (!items.length) {
    vp.innerHTML = `
      <article class="slip-shell kind-note">
        <div class="slip-inner">
          ${amMeshHtml(-1)}
          <div class="slip-body" style="text-align:center;padding:24px 16px;">
            <div class="voice-transcript">「${escapeHtml(p.name)}」的手帐页刚刚翻开，轻点左下角麦克风记下第一笔吧。</div>
          </div>
        </div>
      </article>
    ` + whisperHtml;
  } else {
    vp.innerHTML = items.map((it, i) => renderItemCard(it, i)).join('') + whisperHtml;
  }

  vp.querySelectorAll('[data-open-lb]').forEach((el) => el.addEventListener('click', (e) => {
    if (e.target.closest('[data-quick-bg-mat]') || e.target.closest('[data-fav-id]')) return;
    openLightboxById(el.dataset.openLb);
  }));
  vp.querySelectorAll('[data-quick-bg-mat]').forEach((el) => el.addEventListener('click', (e) => {
    e.stopPropagation();
    promoteMaterialToBackground(p, el.dataset.quickBgMat);
  }));
  vp.querySelectorAll('[data-open-dossier-fact]').forEach((el) => el.addEventListener('click', openDossierSheet));
  vp.querySelectorAll('[data-play-voice]').forEach((el) => el.addEventListener('click', () => toggleVoicePlay(el.dataset.playVoice)));
  vp.querySelectorAll('[data-fav-id]').forEach((el) => el.addEventListener('click', (e) => {
    e.stopPropagation();
    toggleMaterialFavorite(p, el.dataset.favId);
  }));
  vp.querySelectorAll('[data-unveil-id]').forEach((el) => el.addEventListener('click', () => {
    state.unveiledIds.add(el.dataset.unveilId);
    renderFeed();
  }));
  vp.querySelectorAll('[data-open-wheel]').forEach((el) => el.addEventListener('click', () => openDateWheelSheet(el.dataset.openWheel)));
  const qe = vp.querySelector('#btnQuestionEntry');
  if (qe) qe.addEventListener('click', () => openDirectorySheet(false));
}

function renderSeasonFeed(vp, p) {
  vp.classList.remove('is-folio-wings');
  const isSilver = p.tier === 'silver';
  let featDone = false;
  vp.innerHTML = `<div class="season-grid">${p.records.map((rec, idx) => {
    const isPhoto = !isSilver && rec.type === 'photo' && rec.img;
    const isFeat = isPhoto && !featDone;
    if (isFeat) featDone = true;
    const txt = rec.caption || (rec.segments && rec.segments.join('')) || rec.summary || rec.text || '';
    if (isPhoto) {
      const pal = (rec.palette || ['#D8C2A0', '#8B7D6B', '#463D34']).slice(0, 3);
      return `
        <div class="slip-shell season-tile-shell ${isFeat ? 'is-featured' : ''}" data-s-open="${rec.id}" data-s-photo="1">
          <div class="slip-inner season-tile" style="position:relative;">
            <img src="${rec.img}" alt="" style="position:absolute;inset:0;width:100%;height:100%;object-fit:cover;" />
            <div class="photo-slip-scrim"></div>
            <div style="position:relative;z-index:2;padding:10px 12px;display:flex;justify-content:space-between;align-items:center;">
              <span style="font-size:11px;padding:3px 8px;border-radius:999px;background:rgba(14,15,18,0.42);color:#FAF6F0;">${escapeHtml(rec.date)}</span>
              <span style="display:inline-flex;gap:4px;padding:3px 6px;border-radius:999px;background:rgba(14,15,18,0.38);">${pal.map((c) => `<i style="width:6px;height:6px;border-radius:50%;background:${c};display:block;"></i>`).join('')}</span>
            </div>
            <div style="position:relative;z-index:2;padding:10px 12px;color:#FAF6F0;font-family:var(--font-serif);font-size:13px;line-height:1.45;">${escapeHtml(txt.slice(0, 40))}</div>
          </div>
        </div>
      `;
    }
    return `
      <div class="slip-shell season-tile-shell kind-${rec.type === 'voice' ? 'voice' : 'chat'}" data-s-open="${rec.id}">
        <div class="slip-inner season-tile">
          ${amMeshHtml(-(idx * 3 + 2))}
          <div class="slip-body" style="height:100%;justify-content:space-between;">
            <span style="font-size:11px;opacity:0.74;font-family:var(--font-mono);">${escapeHtml(rec.date)} · ${escapeHtml(rec.tag || '手帐')}</span>
            <div style="font-family:var(--font-serif);font-size:13px;line-height:1.52;">${escapeHtml(txt.slice(0, 48))}</div>
          </div>
        </div>
      </div>
    `;
  }).join('')}</div>`;

  vp.querySelectorAll('[data-s-open]').forEach((el) => {
    el.addEventListener('click', () => {
      if (el.dataset.sPhoto) openLightboxById(el.dataset.sOpen);
      else jumpToRecord(el.dataset.sOpen);
    });
  });
}

function renderRingsFeed(vp, p) {
  vp.classList.remove('is-folio-wings');
  const list = p.records;
  if (!list.length) {
    vp.innerHTML = `<div style="text-align:center;opacity:0.7;padding:32px;">暂无光阴年轮节点</div>`;
    return;
  }
  if (!state.selectedRingId || !list.some((r) => r.id === state.selectedRingId)) state.selectedRingId = list[0].id;
  const selIdx = Math.max(0, list.findIndex((r) => r.id === state.selectedRingId));
  const sel = list[selIdx];
  const cx = 200, cy = 200, radii = { 1: 68, 2: 112, 3: 154 };

  const ticks = Array.from({ length: 60 }, (_, i) => {
    const a = (i * 6 - 90) * Math.PI / 180;
    const major = i % 5 === 0;
    const r1 = major ? 166 : 170, r2 = 176;
    return `<line x1="${(cx + Math.cos(a) * r1).toFixed(1)}" y1="${(cy + Math.sin(a) * r1).toFixed(1)}" x2="${(cx + Math.cos(a) * r2).toFixed(1)}" y2="${(cy + Math.sin(a) * r2).toFixed(1)}" stroke="currentColor" stroke-width="${major ? 1.2 : 0.6}" opacity="${major ? 0.36 : 0.16}"/>`;
  }).join('');

  // Spread nodes sharing the same concentric ring evenly so date labels NEVER overlap!
  const ringBuckets = { 1: [], 2: [], 3: [] };
  list.forEach((rec, idx) => {
    const rk = radii[rec.ring] ? rec.ring : ((idx % 3) + 1);
    ringBuckets[rk].push(rec.id);
  });
  const ringOffsets = { 1: 28, 2: 72, 3: 118 };

  const nodes = list.map((rec, idx) => {
    const rk = radii[rec.ring] ? rec.ring : ((idx % 3) + 1);
    const r = radii[rk];
    const bucket = ringBuckets[rk];
    const bIdx = Math.max(0, bucket.indexOf(rec.id));
    const deg = (ringOffsets[rk] + (bIdx * 360) / Math.max(bucket.length, 1)) % 360;
    const rad = (deg - 90) * Math.PI / 180;
    const x = cx + Math.cos(rad) * r, y = cy + Math.sin(rad) * r;
    const isSel = rec.id === sel.id;
    const fill = p.tier === 'silver' ? '#4F6D88' : 'var(--real-warm, #E6BE86)';
    return `
      <g data-ring-id="${rec.id}" style="cursor:pointer;">
        ${isSel ? `<line x1="${cx}" y1="${cy}" x2="${x.toFixed(1)}" y2="${y.toFixed(1)}" stroke="${fill}" stroke-width="1" stroke-dasharray="3 4" opacity="0.72"/>` : ''}
        <circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${isSel ? 15 : 9}" fill="${fill}" opacity="${isSel ? 0.24 : 0.08}"/>
        <circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${isSel ? 6.5 : 4.5}" fill="${fill}" stroke="rgba(14,16,20,0.72)" stroke-width="1.6"/>
        <text x="${(x + (x >= cx ? 11 : -11)).toFixed(1)}" y="${(y + 4).toFixed(1)}" text-anchor="${x >= cx ? 'start' : 'end'}" fill="currentColor" font-size="10.5" font-family="var(--font-mono)" opacity="${isSel ? 0.98 : 0.72}">${escapeHtml(rec.date)}</text>
      </g>
    `;
  }).join('');

  const bodyTxt = sel.caption || (sel.segments && sel.segments.join('')) || sel.summary || sel.text || '';
  vp.innerHTML = `
    <article class="slip-shell kind-chat kind-rings-wide">
      <div class="slip-inner">
        ${amMeshHtml(-4)}
        <div class="slip-body rings-card-body">
          <svg class="rings-svg" viewBox="0 0 400 400" aria-label="光阴年轮星盘">
            ${ticks}
            <circle cx="${cx}" cy="${cy}" r="154" fill="none" stroke="currentColor" stroke-width="0.9" stroke-dasharray="4 5" opacity="0.28"/>
            <circle cx="${cx}" cy="${cy}" r="112" fill="none" stroke="currentColor" stroke-width="0.9" opacity="0.26"/>
            <circle cx="${cx}" cy="${cy}" r="68"  fill="none" stroke="currentColor" stroke-width="1" opacity="0.34"/>
            <circle cx="${cx}" cy="${cy}" r="30"  fill="rgba(255,255,255,0.08)" stroke="currentColor" stroke-width="0.9" opacity="0.45"/>
            <text x="${cx}" y="${cy + 6}" text-anchor="middle" fill="currentColor" font-family="var(--font-serif)" font-size="18">${escapeHtml(p.initial || p.name.slice(0, 1))}</text>
            ${nodes}
          </svg>
          <div class="rings-inspector">
            <div class="s-label"><span>${escapeHtml(sel.date)} · ${escapeHtml(sel.tag || '手帐')}</span><span>${selIdx + 1} / ${list.length}</span></div>
            ${p.tier !== 'silver' && sel.type === 'photo' && sel.img ? `<img class="rings-thumb" src="${sel.img}" alt="" />` : ''}
            <div class="voice-transcript">${escapeHtml(bodyTxt)}</div>
            <div style="display:flex;gap:8px;margin-top:4px;">
              <button class="s-btn-primary" id="btnRingOpen" type="button" style="height:34px;font-size:12px;">${p.tier !== 'silver' && sel.type === 'photo' && sel.img ? '暗房观片 ↗' : '定位原件 ↗'}</button>
              <button class="s-btn-ghost" id="btnRingPrev" type="button" style="height:34px;font-size:12px;">← 上一刻</button>
              <button class="s-btn-ghost" id="btnRingNext" type="button" style="height:34px;font-size:12px;">下一刻 →</button>
            </div>
          </div>
        </div>
      </div>
    </article>
  `;
  vp.querySelectorAll('[data-ring-id]').forEach((g) => g.addEventListener('click', () => { state.selectedRingId = g.dataset.ringId; renderFeed(); }));
  vp.querySelector('#btnRingOpen').addEventListener('click', () => {
    if (p.tier !== 'silver' && sel.type === 'photo' && sel.img) openLightboxById(sel.id);
    else jumpToRecord(sel.id);
  });
  vp.querySelector('#btnRingPrev').addEventListener('click', () => { state.selectedRingId = list[(selIdx - 1 + list.length) % list.length].id; renderFeed(); });
  vp.querySelector('#btnRingNext').addEventListener('click', () => { state.selectedRingId = list[(selIdx + 1) % list.length].id; renderFeed(); });
}

function renderFeed() {
  const vp = document.getElementById('feedViewport');
  const p = currentPerson();
  if (!p) return;
  renderTopAndMasthead();
  if (state.scale === 'folio') renderFolioFeed(vp, p);
  else if (state.scale === 'season') renderSeasonFeed(vp, p);
  else renderRingsFeed(vp, p);
}

function toggleVoicePlay(recId) {
  if (state.playingVoiceId === recId) {
    clearInterval(state.voiceTimer);
    state.playingVoiceId = null;
    state.voiceProgress = 0;
    renderFeed();
    return;
  }
  clearInterval(state.voiceTimer);
  state.playingVoiceId = recId;
  state.voiceProgress = 0.08;
  renderFeed();
  state.voiceTimer = setInterval(() => {
    state.voiceProgress += 0.08;
    if (state.voiceProgress >= 1) {
      clearInterval(state.voiceTimer);
      state.playingVoiceId = null;
      state.voiceProgress = 0;
    }
    renderFeed();
  }, 320);
}

function jumpToRecord(recId) {
  closeCupid();
  closeAllSheets();
  closeSpotlight();
  state.scale = 'folio';
  renderFeed();
  setTimeout(() => {
    const el = document.querySelector(`[data-rec-id="${recId}"]`);
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      el.classList.add('is-jump-flash');
      setTimeout(() => el.classList.remove('is-jump-flash'), 1200);
    }
  }, 60);
}

function scrollMainToTop(smooth = false) {
  const sc = document.getElementById('mainScroller');
  const root = document.documentElement;
  const behavior = smooth ? 'smooth' : 'instant';
  if (sc && (root.classList.contains('v15-iphone-sim') || root.classList.contains('is-native-ios') || window.innerWidth <= 768)) {
    sc.scrollTo({ top: 0, behavior });
  } else {
    window.scrollTo({ top: 0, behavior });
  }
}

function selectPerson(pid) {
  state.personId = pid;
  state.scale = 'folio';
  state.selectedRingId = null;
  state.cupidAttachedIds.clear();
  renderApp();
  scrollMainToTop(false);
}

/* ============================================================================
   6. LIVE MUTATIONS: FAVORITE, BACKGROUND PROMOTE, TIER SWITCH, PHOTO UPLOAD
   ============================================================================ */
async function toggleMaterialFavorite(p, recId) {
  const rec = p.records.find((r) => r.id === recId);
  if (!rec) return;
  const nextFav = !rec.fav;
  rec.fav = nextFav;
  renderFeed();
  if (document.getElementById('lightboxOverlay').classList.contains('is-open')) renderLightbox();

  try {
    const updated = await apiJson(`/materials/${recId}`, {
      method: 'PATCH',
      headers: { 'If-Match': `"${rec.version}"` },
      body: { isFavorite: nextFav },
    });
    if (updated) {
      rec.version = updated.version;
      rec.fav = Boolean(updated.isFavorite);
    }
    showToast(nextFav ? '★ 已珍藏并同步云端' : '已取消珍藏');
  } catch (err) {
    rec.fav = !nextFav;
    renderFeed();
    showToast(err.message || '同步珍藏状态失败');
  }
}

async function promoteMaterialToBackground(p, materialId) {
  if (p.tier === 'silver') {
    showToast('银箭人物遵循零照片隐私护盾，不设背景摄影');
    return;
  }
  try {
    const updated = await apiJson(`/persons/${p.id}/background:from-material`, {
      method: 'POST',
      body: { materialId },
    });
    if (updated) {
      p.mainPhotoUrl = updated.mainPhotoUrl ? stabilizeSignedUrl(updated.mainPhotoUrl, true) : p.mainPhotoUrl;
      p.visualTokens = updated.visualTokens || p.visualTokens;
      p.activeHeroMaterialId = materialId;
      p.version = updated.version || p.version;
      bumpPhotoCacheGen(p.id);
      syncChromaForPerson(p);
      renderTriDockSeals();
      renderFeed();
      if (document.getElementById('lightboxOverlay').classList.contains('is-open')) renderLightbox();
      if (document.getElementById('dossierSheet').classList.contains('is-open')) openDossierSheet();
      showToast('✦ 已映作背景并实时提取真原图琉璃色域');
    }
  } catch (err) {
    showToast(err.message || '切换背景相纸失败');
  }
}

async function uploadDirectHeroPhoto(p, file) {
  if (!p || !file) return;
  if (p.tier === 'silver') {
    showToast('银箭人物遵循零照片隐私护盾，不设背景摄影');
    return;
  }
  const fd = new FormData();
  fd.append('file', file);
  try {
    const updated = await apiJson(`/persons/${p.id}/photos/main`, { method: 'POST', body: fd });
    if (updated) {
      p.mainPhotoUrl = stabilizeSignedUrl(updated.mainPhotoUrl, true);
      p.visualTokens = updated.visualTokens || p.visualTokens;
      p.version = updated.version || p.version;
      bumpPhotoCacheGen(p.id);
      syncChromaForPerson(p);
      renderTriDockSeals();
      renderFeed();
      if (document.getElementById('dossierSheet').classList.contains('is-open')) openDossierSheet();
      showToast('✦ 已上传背景摄影并自动重算琉璃色域');
    }
  } catch (err) {
    showToast(err.message || '上传背景摄影失败');
  }
}

async function switchPersonTier(tier) {
  const p = currentPerson();
  if (!p || p.tier === tier) return;
  try {
    const updated = await apiJson(`/persons/${p.id}/tier`, {
      method: 'PATCH',
      headers: { 'If-Match': `"${p.version}"` },
      body: { tier },
    });
    if (updated) {
      await refreshBootstrap({ keepPersonId: true });
      showToast(`已将「${p.name}」切换为 ${TIER_META[tier].full}`);
    }
  } catch (err) {
    showToast(err.message || '切换箭矢层级失败');
  }
}

/* ============================================================================
   7. SCENE 6: LIVE FULL-SCREEN CUPID COMPANION CHAT (SSE + MATERIAL DRAWER)
   ============================================================================ */
async function openCupid() {
  const p = currentPerson();
  if (!p) return;
  closeAllSheets();
  closeSpotlight();
  const staleToast = document.getElementById('toastPill');
  if (staleToast) staleToast.classList.remove('is-show');
  document.getElementById('cupidHeadTitle').textContent = p.tier === 'silver' ? `丘比特 · ${p.name}（同盟）` : `丘比特 · ${p.name}`;
  document.getElementById('cupidHeadTierImg').src = ARROW_PINS[p.tier] || ARROW_PINS.gold;
  document.getElementById('cupidMatCount').textContent = `手帐 ${p.records.length}`;
  document.getElementById('cupidChatInput').placeholder = p.tier === 'silver'
    ? '尽管跟丘比特吐吐槽，完全站你这边…'
    : `和丘比特说说关于「${p.name}」…`;
  document.getElementById('cupidFullscreen').classList.add('is-open');
  renderMatDrawer();
  renderCupidChat();

  if (!state.cupidLoaded.has(p.id)) {
    try {
      const page = await apiJson(`/persons/${p.id}/chat/messages?limit=50&order=asc`);
      const msgs = (page && Array.isArray(page.items)) ? page.items : [];
      state.cupidMessages[p.id] = msgs.map((m) => ({
        id: m.id,
        role: m.role === 'user' ? 'user' : 'ai',
        text: m.content || '',
        whisper: m.whisperNote || '',
        perspectives: p.tier === 'gold' && Array.isArray(m.perspectives) ? m.perspectives : [],
        citations: Array.isArray(m.citations) ? m.citations : [],
        suggestions: Array.isArray(m.suggestions) ? m.suggestions : [],
        crisis: m.crisisCard || null,
      }));
      state.cupidLoaded.add(p.id);
      renderCupidChat();
    } catch (_) {}
  }
}

function closeCupid() {
  document.getElementById('cupidFullscreen').classList.remove('is-open');
  closeMatDrawer();
}

function openMatDrawer() {
  renderMatDrawer();
  document.getElementById('cupidMatScrim').classList.add('is-open');
  document.getElementById('cupidMatDrawer').classList.add('is-open');
}

function closeMatDrawer() {
  document.getElementById('cupidMatScrim').classList.remove('is-open');
  document.getElementById('cupidMatDrawer').classList.remove('is-open');
}

function renderMatDrawer() {
  const p = currentPerson();
  if (!p) return;
  document.getElementById('matDrawerTitle').textContent = `${p.name}的手帐素材`;
  const list = document.getElementById('matDrawerList');
  list.innerHTML = p.records.map((r) => {
    const picked = state.cupidAttachedIds.has(r.id);
    const txt = r.caption || (r.segments && r.segments.join('')) || r.summary || r.text || '';
    const icon = r.type === 'voice' ? '🎙' : r.type === 'photo' ? '🖼' : r.type === 'chat' ? '💬' : r.type === 'shield-log' ? '🛡' : r.type === 'draft' ? '✎' : '✦';
    return `
      <button class="drawer-item ${picked ? 'is-picked' : ''}" data-pick-mat="${r.id}" type="button">
        <div style="display:flex;justify-content:space-between;font-size:11px;opacity:0.72;">
          <span>${icon} ${escapeHtml(r.date)} · ${escapeHtml(r.tag || '手帐')}</span>
          <span>${picked ? '✓ 已引用' : '+ 引用'}</span>
        </div>
        <div>${escapeHtml(txt.slice(0, 56))}</div>
      </button>
    `;
  }).join('');
  list.querySelectorAll('[data-pick-mat]').forEach((b) => {
    b.addEventListener('click', () => {
      const id = b.dataset.pickMat;
      if (state.cupidAttachedIds.has(id)) state.cupidAttachedIds.delete(id);
      else state.cupidAttachedIds.add(id);
      renderMatDrawer();
      renderAttachedTray();
    });
  });
}

function renderAttachedTray() {
  const p = currentPerson();
  if (!p) return;
  const tray = document.getElementById('cupidAttachedTray');
  const ids = [...state.cupidAttachedIds];
  if (!ids.length) { tray.innerHTML = ''; return; }
  tray.innerHTML = ids.map((id) => {
    const r = p.records.find((x) => x.id === id);
    if (!r) return '';
    return `<span class="cupid-attached-chip"><span>📎 ${escapeHtml(r.date)} ${escapeHtml(r.tag || '手帐')}</span><button type="button" data-rm-att="${id}" style="opacity:0.75;">✕</button></span>`;
  }).join('');
  tray.querySelectorAll('[data-rm-att]').forEach((b) => b.addEventListener('click', () => {
    state.cupidAttachedIds.delete(b.dataset.rmAtt);
    renderAttachedTray();
    renderMatDrawer();
  }));
}

function renderCupidChat() {
  const p = currentPerson();
  if (!p) return;
  const thread = state.cupidMessages[p.id] || [];
  const citeIcon = (tid) => {
    const r = p.records.find((x) => x.id === tid);
    if (!r) return '✦';
    return r.type === 'voice' ? '🎙' : r.type === 'photo' ? '🖼' : r.type === 'chat' ? '💬' : r.type === 'shield-log' ? '🛡' : r.type === 'draft' ? '✎' : '✦';
  };

  const body = document.getElementById('cupidChatBody');
  const traces = p.records.slice(0, 3).map((r) => `${r.date} ${r.tag}：${(r.caption || r.text || r.summary || '').slice(0, 44)}`);
  const introLetter = p.tier === 'silver'
    ? `已为你备妥「${p.name}」的 ${p.records.length} 条加密边界留痕。这里是完全站你这边的私密树洞——想吐槽、想复盘时间线，还是想拟一封不卑不亢的书面回复，直接开口就好。`
    : p.tier === 'crystal'
      ? `已翻阅「${p.name}」的 ${p.records.length} 条往来备忘与 ${p.facts.length} 项规矩玉签。需要核对细节或起草一段干脆礼貌的消息，随时告诉我。`
      : `已翻阅「${p.name}」的 ${p.records.length} 条真实日常手帐与 ${p.facts.length} 项小习惯。你可以直接提问，或点右上角「手帐」抽屉附带某条原件来聊。`;

  const lastAi = [...thread].reverse().find((m) => m.role === 'ai');
  const lastIsCrisis = Boolean(lastAi && lastAi.crisis);
  const prompts = lastIsCrisis ? [] : ((lastAi && lastAi.suggestions && lastAi.suggestions.length) ? lastAi.suggestions : p.cupidPrompts);

  body.innerHTML = `
    <div style="text-align:center;font-size:11.5px;opacity:0.62;">已核对 ${p.records.length} 条实库加密手帐 · ${p.facts.length} 项记住的小事</div>

    ${traces.length ? `
      <div class="cupid-trace-pill">
        <button class="cupid-trace-toggle" id="btnCupidTrace" type="button">
          <span>✦ 已载入「${escapeHtml(p.name)}」近期 ${traces.length} 组手帐原件</span>
          <span>${state.cupidTraceOpen ? '收起 ▴' : '查看原件 ▾'}</span>
        </button>
        <div class="cupid-trace-list ${state.cupidTraceOpen ? '' : 'is-collapsed'}">
          ${traces.map((t) => `<div>· ${escapeHtml(t)}</div>`).join('')}
        </div>
      </div>
    ` : ''}

    <div class="cupid-msg is-ai">
      <div>${escapeHtml(introLetter)}</div>
    </div>

    ${thread.map((m) => {
      if (m.role === 'user') {
        return `<div class="cupid-msg is-user">${m.refs ? `<div style="font-size:11px;opacity:0.75;margin-bottom:4px;">📎 引用了 ${escapeHtml(m.refs)}</div>` : ''}${escapeHtml(m.text)}</div>`;
      }
      if (m.crisis) {
        const hotlines = (m.crisis.hotlines || []).map((h) => `<a class="cupid-crisis-link" href="tel:${escapeHtml(h.phone || h.number || '')}"><span>${escapeHtml(h.name || h.label || '援助热线')}</span><span>${escapeHtml(h.phone || h.number || '')}</span></a>`).join('');
        return `<div class="cupid-crisis-card"><strong style="font-size:14px;">${escapeHtml(m.crisis.title || '先让我陪着你')}</strong><div style="font-size:13px;line-height:1.6;">${escapeHtml(m.crisis.careMessage || '')}</div><div class="cupid-crisis-hotlines">${hotlines}</div></div>`;
      }
      const cits = (m.citations || []).map((ct) => {
        const mid = ct.materialId || ct.targetId || '';
        const lbl = ct.title || ct.label || '手帐原件';
        return `<button class="cupid-cite-chip" data-cite-jump="${escapeHtml(mid)}" type="button"><span>${citeIcon(mid)}</span><span>${escapeHtml(lbl.slice(0, 18))}</span><span>↗</span></button>`;
      }).join('');
      const perspHtml = p.tier === 'gold' && m.perspectives && m.perspectives.length
        ? `<div class="cupid-perspective-card" style="margin-top:10px;">
             <div style="font-size:11.5px;font-weight:600;opacity:0.86;margin-bottom:4px;">✦ 换个角度想想 · 金箭专属</div>
             <div style="opacity:0.90;">${m.perspectives.map((pt) => escapeHtml(pt)).join('；')}</div>
           </div>`
        : '';
      return `<div class="cupid-msg is-ai">
        <div>${escapeHtml(m.text)}</div>
        ${cits ? `<div class="cupid-cites-row">${cits}</div>` : ''}
        ${m.whisper ? `<div class="cupid-whisper-line">${escapeHtml(m.whisper)}</div>` : ''}
        ${perspHtml}
      </div>`;
    }).join('')}

    ${state.cupidThinking ? `<div style="font-size:12.5px;opacity:0.80;padding:4px 8px;">✦ ${p.tier === 'silver' ? '正在核对时间线并起草回应…' : '丘比特正在翻阅手帐原件…'}</div>` : ''}

    <div class="cupid-prompts-strip">
      ${prompts.map((q) => `<button class="cupid-prompt-chip" data-ask-q="${escapeHtml(q)}" type="button">${escapeHtml(q)}</button>`).join('')}
      ${thread.length ? `<button class="cupid-prompt-chip" id="btnClearCupidThread" type="button" style="opacity:0.65;">清空对话</button>` : ''}
    </div>
  `;

  document.getElementById('cupidShimmerShell').classList.toggle('is-thinking', state.cupidThinking);
  renderAttachedTray();

  const tb = body.querySelector('#btnCupidTrace');
  if (tb) tb.addEventListener('click', () => { state.cupidTraceOpen = !state.cupidTraceOpen; renderCupidChat(); });
  body.querySelectorAll('[data-cite-jump]').forEach((b) => b.addEventListener('click', () => {
    if (b.dataset.citeJump) jumpToRecord(b.dataset.citeJump);
  }));
  body.querySelectorAll('[data-ask-q]').forEach((b) => b.addEventListener('click', () => sendCupidQuestion(b.dataset.askQ)));
  const clr = body.querySelector('#btnClearCupidThread');
  if (clr) {
    clr.addEventListener('click', async () => {
      try {
        await apiJson(`/persons/${p.id}/chat/messages`, { method: 'DELETE' });
        state.cupidMessages[p.id] = [];
        renderCupidChat();
        showToast('已清空本页丘比特对话记录');
      } catch (err) {
        showToast(err.message || '清空失败');
      }
    });
  }
  body.scrollTop = body.scrollHeight;
}

async function sendCupidQuestion(qText) {
  const p = currentPerson();
  if (!p || state.cupidThinking) return;
  const attachedRecs = [...state.cupidAttachedIds].map((id) => p.records.find((r) => r.id === id)).filter(Boolean);
  const q = (qText || '').trim() || (attachedRecs.length ? `结合我引用的这${attachedRecs.length}条手帐，帮我出出主意吧？` : '');
  if (!q) return;

  if (!state.cupidMessages[p.id]) state.cupidMessages[p.id] = [];
  const refStr = attachedRecs.map((r) => `${r.date} ${r.tag || '手帐'}`).join('、');
  const validMaterialIds = attachedRecs.map((r) => r.id).filter((id) => UUID_RE.test(id));

  state.cupidMessages[p.id].push({ role: 'user', text: q, refs: refStr });
  const aiMsg = { role: 'ai', text: '', whisper: '', perspectives: [], citations: [], suggestions: [], crisis: null };
  state.cupidMessages[p.id].push(aiMsg);

  state.cupidAttachedIds.clear();
  closeMatDrawer();
  state.cupidThinking = true;
  renderCupidChat();

  try {
    const resp = await apiFetch(`/persons/${p.id}/chat/stream`, {
      method: 'POST',
      headers: { Accept: 'text/event-stream' },
      body: {
        message: q,
        referencedMaterialIds: validMaterialIds,
        clientMessageId: makeUuid(),
      },
    });
    if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);

    const reader = resp.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';

    const handleBlock = (block) => {
      const lines = block.split(/\r?\n/);
      let evName = 'message';
      const dataLines = [];
      for (const ln of lines) {
        if (ln.startsWith('event:')) evName = ln.slice(6).trim();
        else if (ln.startsWith('data:')) dataLines.push(ln.slice(5).trim());
      }
      if (!dataLines.length) return;
      let payload = null;
      try { payload = JSON.parse(dataLines.join('\n')); } catch (_) { return; }

      if (evName === 'delta' && payload?.text) {
        aiMsg.text += payload.text;
        renderCupidChat();
      } else if (evName === 'citation' && payload?.materialId) {
        if (!aiMsg.citations.some((c) => c.materialId === payload.materialId)) {
          aiMsg.citations.push(payload);
        }
        renderCupidChat();
      } else if (evName === 'whisper' && payload?.text) {
        aiMsg.whisper = payload.text;
        renderCupidChat();
      } else if (evName === 'perspective' && p.tier === 'gold' && Array.isArray(payload?.items)) {
        aiMsg.perspectives = payload.items;
        renderCupidChat();
      } else if (evName === 'suggestion' && Array.isArray(payload?.items)) {
        aiMsg.suggestions = payload.items;
        renderCupidChat();
      } else if (evName === 'crisis' && payload) {
        aiMsg.crisis = payload;
        renderCupidChat();
      }
    };

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let sep = buffer.indexOf('\n\n');
      while (sep !== -1) {
        const blk = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        if (blk.trim()) handleBlock(blk);
        sep = buffer.indexOf('\n\n');
      }
    }
    if (buffer.trim()) handleBlock(buffer);
    if (!aiMsg.text && !aiMsg.crisis) {
      aiMsg.text = '丘比特刚刚收到了你的话，正在整理思路，再发一次试试吧。';
    }
  } catch (err) {
    aiMsg.text = aiMsg.text || `网络轻晃了一下（${err.message}），稍后再试。`;
  } finally {
    state.cupidThinking = false;
    renderCupidChat();
  }
}

/* ============================================================================
   8. LIQUID GLASS SHEETS (DOSSIER, DIRECTORY, CAPTURE, SETTINGS, LUNAR WHEEL)
   ============================================================================ */
function openSheet(id) {
  closeAllSheets();
  closeSpotlight();
  state.sheetOpenedAt = Date.now();
  document.getElementById('sheetBackdrop').classList.add('is-open');
  document.getElementById(id).classList.add('is-open');
}

function closeAllSheets() {
  document.getElementById('sheetBackdrop').classList.remove('is-open');
  document.querySelectorAll('.glass-sheet.is-open').forEach((s) => s.classList.remove('is-open'));
  if (state.capWaveRaf) { cancelAnimationFrame(state.capWaveRaf); state.capWaveRaf = null; }
}

/* Sheet 1: 人物档案口袋 */
function openDossierSheet() {
  const p = currentPerson();
  if (!p) return;
  document.getElementById('dossierSheetTitle').textContent = `${p.name} · 人物档案`;
  const body = document.getElementById('dossierSheetBody');
  const photoMats = p.tier === 'silver' ? [] : p.records.filter((r) => r.type === 'photo' && r.img);

  body.innerHTML = `
    <div class="s-card">
      <div class="s-label"><span>近况简报</span><span>${TIER_META[p.tier]?.full || ''}</span></div>
      <div style="font-family:var(--font-serif);font-size:14px;line-height:1.68;">${escapeHtml(p.shortMemo || p.contextSub || `已为「${p.name}」建立独立加密关系档案。`)}</div>
      <button class="s-btn-primary" id="btnDossierToCupid" type="button" style="width:fit-content;height:34px;font-size:12.5px;">让丘比特帮我准备 →</button>
    </div>

    ${p.tier !== 'silver' ? `
      <div class="s-card">
        <div class="s-label">
          <span>背景摄影相纸</span>
          <button class="s-pill is-active" id="btnDossierUploadBg" type="button" style="height:24px;font-size:11px;">＋ 上传照片</button>
        </div>
        ${photoMats.length ? `
          <div class="bg-picker-strip">
            ${photoMats.map((ph) => `
              <button class="bg-pick-btn ${ph.id === p.activeHeroMaterialId ? 'is-active' : ''}" data-bg-mat-id="${ph.id}" type="button">
                <img src="${ph.img}" alt="${escapeHtml(ph.scene_label || ph.tag)}" />
                <span>${escapeHtml((ph.scene_label || ph.tag || '相片').slice(0, 8))}</span>
              </button>
            `).join('')}
          </div>
        ` : `<div style="font-size:12px;opacity:0.7;">上传一张人物摄影或生活留影，心络将实时提取真原图琉璃色域。</div>`}
      </div>
    ` : `
      <div class="s-card">
        <div class="s-label"><span>银箭零照片隐私护盾</span><span>已强制启用</span></div>
        <div style="font-size:12.5px;line-height:1.6;opacity:0.82;">银箭人物不设背景摄影、不渲染彩墨流光，所有边界留痕默认由长虹玻璃幕布收起。</div>
      </div>
    `}

    <div class="s-card">
      <div class="s-label">
        <span>记挂的日子 · 农历/公历双历</span>
        <button class="s-pill" id="btnDossierAddDate" type="button" style="height:24px;font-size:11px;">＋ 核对农历</button>
      </div>
      ${p.care_days.length ? p.care_days.map((cd) => `
        <div style="display:flex;flex-direction:column;gap:8px;padding:10px;border-radius:13px;background:rgba(255,255,255,0.06);border:0.75px solid rgba(255,255,255,0.12);">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">
            <div>
              <div style="font-size:13.5px;font-weight:600;">${escapeHtml(cd.title)}</div>
              <div style="font-size:11.5px;opacity:0.68;margin-top:2px;">${escapeHtml(cd.date_label)} · ${escapeHtml(cd.lunar_label)} · ${escapeHtml(cd.days_left)}</div>
            </div>
            <button class="s-pill ${cd.done ? 'is-active' : ''}" data-cd-done="${cd.id}" type="button">${cd.done ? '✓ 记完了' : '记完了'}</button>
          </div>
          <div class="s-pill-row">
            ${[['提前 7 天', [7, 1, 0]], ['提前 1 天', [1, 0]], ['当天', [0]]].map(([lbl, arr]) =>
              `<button class="s-pill ${cd.lead === lbl ? 'is-active' : ''}" data-cd-lead="${cd.id}" data-lead-arr="${arr.join(',')}" type="button">${lbl}</button>`
            ).join('')}
          </div>
        </div>
      `).join('') : `<div style="font-size:12.5px;opacity:0.66;">还没有记挂日程，点击右上角「核对农历」添入第一条。</div>`}
    </div>

    <div class="s-card">
      <div class="s-label"><span>记住的小事 · 实库玉签</span><span>${p.facts.length} 项</span></div>
      ${p.facts.map((f, fi) => `
        <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;padding:7px 0;border-top:${fi ? '0.75px solid rgba(255,255,255,0.10)' : 'none'};">
          <div style="font-size:13px;line-height:1.5;opacity:${f.faded ? 0.42 : 0.94};text-decoration:${f.faded ? 'line-through' : 'none'};">
            <strong style="margin-right:6px;opacity:0.74;">${escapeHtml(f.kind)}</strong>${escapeHtml(f.text)}
          </div>
          <button class="s-pill" data-fact-toggle="${f.id}" type="button">${f.faded ? '恢复' : '淡忘'}</button>
        </div>
      `).join('')}
      <form id="addFactForm" style="display:flex;gap:8px;margin-top:6px;">
        <input class="spot-input" id="newFactInput" type="text" placeholder="添记一条小习惯或忌口…" style="flex:1;padding:7px 11px;border-radius:11px;background:rgba(255,255,255,0.08);border:0.75px solid rgba(255,255,255,0.16);" />
        <button class="s-pill is-active" type="submit">落墨</button>
      </form>
    </div>
  `;

  body.querySelector('#btnDossierToCupid').addEventListener('click', openCupid);
  const upBtn = body.querySelector('#btnDossierUploadBg');
  if (upBtn) {
    upBtn.addEventListener('click', () => document.getElementById('dossierBgUploadInput').click());
  }
  body.querySelectorAll('[data-bg-mat-id]').forEach((b) => b.addEventListener('click', () => {
    promoteMaterialToBackground(p, b.dataset.bgMatId);
  }));
  body.querySelector('#btnDossierAddDate').addEventListener('click', () => openDateWheelSheet(null));

  body.querySelectorAll('[data-cd-done]').forEach((b) => b.addEventListener('click', async () => {
    const cd = p.care_days.find((x) => x.id === b.dataset.cdDone);
    if (!cd) return;
    const nextStatus = cd.done ? 'active' : 'completed';
    try {
      await apiJson(`/reminders/${cd.id}`, {
        method: 'PATCH',
        headers: { 'If-Match': `"${cd.version}"` },
        body: { status: nextStatus },
      });
      await refreshBootstrap({ keepPersonId: true });
      openDossierSheet();
    } catch (err) { showToast(err.message || '更新提醒失败'); }
  }));

  body.querySelectorAll('[data-cd-lead]').forEach((b) => b.addEventListener('click', async () => {
    const cd = p.care_days.find((x) => x.id === b.dataset.cdLead);
    if (!cd) return;
    const leadDays = b.dataset.leadArr.split(',').map(Number);
    try {
      await apiJson(`/reminders/${cd.id}`, {
        method: 'PATCH',
        headers: { 'If-Match': `"${cd.version}"` },
        body: { leadDays },
      });
      await refreshBootstrap({ keepPersonId: true });
      openDossierSheet();
      showToast(`提醒已调整为「${leadDaysLabel(leadDays)}」`);
    } catch (err) { showToast(err.message || '更新提前天数失败'); }
  }));

  body.querySelectorAll('[data-fact-toggle]').forEach((b) => b.addEventListener('click', async () => {
    const f = p.facts.find((x) => x.id === b.dataset.factToggle);
    if (!f) return;
    const nextStatus = f.faded ? 'confirmed' : 'rejected';
    try {
      await apiJson(`/facts/${f.id}`, {
        method: 'PATCH',
        headers: { 'If-Match': `"${f.version}"` },
        body: { status: nextStatus },
      });
      await refreshBootstrap({ keepPersonId: true });
      openDossierSheet();
      showToast(nextStatus === 'rejected' ? '已淡忘此条玉签' : '已恢复玉签');
    } catch (err) { showToast(err.message || '更新玉签失败'); }
  }));

  body.querySelector('#addFactForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const inp = body.querySelector('#newFactInput');
    const txt = (inp.value || '').trim();
    if (!txt) return;
    try {
      await apiJson(`/persons/${p.id}/facts`, {
        method: 'POST',
        body: { type: 'preference', polarity: 'positive', content: txt },
      });
      await refreshBootstrap({ keepPersonId: true });
      openDossierSheet();
      showToast('✦ 已加密落墨至人物玉签');
    } catch (err) { showToast(err.message || '添加玉签失败'); }
  });

  openSheet('dossierSheet');
}

/* Sheet 2: 人物名册与记繁琐 + 72h 冷静区 + 新添人物 */
function openDirectorySheet(focusNew = false) {
  if (focusNew) state.showNewPersonForm = true;
  renderDirectorySheet();
  openSheet('directorySheet');
}

function renderDirectorySheet() {
  const body = document.getElementById('directorySheetBody');
  const list = state.people.filter((p) => state.dirFilter === 'all' || p.tier === state.dirFilter);
  const coolingItems = state.inboxItems.filter((it) => it.kind === 'venting_hold');
  const disambigItems = state.inboxItems.filter((it) => it.kind !== 'venting_hold');

  body.innerHTML = `
    ${state.showNewPersonForm ? `
      <form class="s-card" id="newPersonForm">
        <div class="s-label"><span>新添一页手帐人物</span><button type="button" id="btnCancelNewP" style="opacity:0.7;">收起</button></div>
        <input class="spot-input" id="newPName" type="text" placeholder="姓名（例如：妈妈 / 表姨 / 张学长）" style="padding:9px 12px;border-radius:12px;background:rgba(255,255,255,0.08);border:0.75px solid rgba(255,255,255,0.16);" required />
        <input class="spot-input" id="newPRole" type="text" placeholder="关系身份（选填，如：母亲 / 伴侣 / 项目同事）" style="padding:9px 12px;border-radius:12px;background:rgba(255,255,255,0.08);border:0.75px solid rgba(255,255,255,0.16);" />
        <input class="spot-input" id="newPMemo" type="text" placeholder="一句初印象或小习惯（选填）" style="padding:9px 12px;border-radius:12px;background:rgba(255,255,255,0.08);border:0.75px solid rgba(255,255,255,0.16);" />
        <div class="s-pill-row" id="newPTierRow">
          <button class="s-pill is-active" data-new-tier="gold" type="button">金箭 · 记爱</button>
          <button class="s-pill" data-new-tier="crystal" type="button">水晶箭 · 记繁琐</button>
          <button class="s-pill" data-new-tier="silver" type="button">银箭 · 记恨</button>
        </div>
        <button class="s-btn-primary" type="submit">添入名册并建页</button>
      </form>
    ` : ''}

    <div class="s-pill-row">
      <button class="s-pill ${state.dirFilter === 'all' ? 'is-active' : ''}" data-df="all" type="button">全部 (${state.people.length})</button>
      <button class="s-pill ${state.dirFilter === 'gold' ? 'is-active' : ''}" data-df="gold" type="button">金箭 (${state.people.filter((x) => x.tier === 'gold').length})</button>
      <button class="s-pill ${state.dirFilter === 'crystal' ? 'is-active' : ''}" data-df="crystal" type="button">水晶箭 (${state.people.filter((x) => x.tier === 'crystal').length})</button>
      <button class="s-pill ${state.dirFilter === 'silver' ? 'is-active' : ''}" data-df="silver" type="button">银箭 (${state.people.filter((x) => x.tier === 'silver').length})</button>
    </div>

    <div style="display:flex;flex-direction:column;gap:8px;">
      ${list.map((p) => {
        const imgUrl = p.tier !== 'silver' && p.mainPhotoUrl ? p.mainPhotoUrl : null;
        return `
          <button class="dir-person-row ${p.id === state.personId ? 'is-active' : ''}" data-pick-person="${p.id}" type="button">
            <span class="dir-avatar">${imgUrl ? `<img src="${imgUrl}" alt="" />` : escapeHtml(p.initial || p.name.slice(0, 1))}</span>
            <span style="flex:1;min-width:0;">
              <div style="display:flex;align-items:center;gap:6px;">
                <strong style="font-size:14px;">${escapeHtml(p.name)}</strong>
                <span style="font-size:11px;opacity:0.68;">${escapeHtml(p.role)}</span>
              </div>
              <div style="font-size:11.5px;opacity:0.65;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px;">${escapeHtml(p.shortMemo || p.contextSub || `${p.records.length} 条记录`)}</div>
            </span>
            <span style="font-size:11.5px;opacity:0.65;">${p.records.length} 页 ›</span>
          </button>
        `;
      }).join('')}
    </div>

    ${disambigItems.length ? disambigItems.map((it) => `
      <div class="s-card">
        <div class="s-label"><span>待确认归属</span><span>${escapeHtml(it.mention || '新人物')}</span></div>
        <div style="font-size:13px;line-height:1.55;">${escapeHtml(it.summary || '')}</div>
        <div class="s-pill-row">
          <button class="s-pill is-active" data-inbox-create="${it.id}" data-inbox-name="${escapeHtml(it.mention || '新人物')}" type="button">新建人物并归档</button>
          <button class="s-pill" data-inbox-discard="${it.id}" type="button">暂不归档</button>
        </div>
      </div>
    `).join('') : ''}

    <div class="s-card">
      <div class="s-label"><span>72h 冷静区 · 金箭关系保护</span><span>${coolingItems.length} 条</span></div>
      ${coolingItems.length ? coolingItems.map((cz) => `
        <div style="font-size:12.5px;line-height:1.58;opacity:0.86;">
          <div style="font-size:11px;opacity:0.65;margin-bottom:3px;">${escapeHtml(cz.personName || '金箭保护')} · 72h 情绪缓冲</div>
          <div>${escapeHtml(cz.summary || '')}</div>
          <div class="s-pill-row" style="margin-top:8px;">
            <button class="s-pill" data-inbox-discard="${cz.id}" type="button">释怀淡忘</button>
            <button class="s-pill" data-inbox-keep="${cz.id}" type="button">仍写入手帐</button>
          </div>
        </div>
      `).join('') : `<div style="font-size:12.5px;opacity:0.65;">当前无情绪气话暂存，你的关系档案保持温润清澈。</div>`}
    </div>
  `;

  body.querySelectorAll('[data-df]').forEach((b) => b.addEventListener('click', () => { state.dirFilter = b.dataset.df; renderDirectorySheet(); }));
  body.querySelectorAll('[data-pick-person]').forEach((b) => b.addEventListener('click', () => { selectPerson(b.dataset.pickPerson); closeAllSheets(); }));
  body.querySelectorAll('[data-inbox-discard]').forEach((b) => b.addEventListener('click', async () => {
    try {
      await apiJson(`/inbox/${b.dataset.inboxDiscard}:resolve`, { method: 'POST', body: { action: 'discard' } });
      await refreshBootstrap({ keepPersonId: true });
      renderDirectorySheet();
      showToast('已释怀移除');
    } catch (err) { showToast(err.message || '操作失败'); }
  }));
  body.querySelectorAll('[data-inbox-keep]').forEach((b) => b.addEventListener('click', async () => {
    try {
      await apiJson(`/inbox/${b.dataset.inboxKeep}:resolve`, { method: 'POST', body: { action: 'keep_as_note' } });
      await refreshBootstrap({ keepPersonId: true });
      renderDirectorySheet();
      showToast('已转入正式手帐');
    } catch (err) { showToast(err.message || '操作失败'); }
  }));
  body.querySelectorAll('[data-inbox-create]').forEach((b) => b.addEventListener('click', async () => {
    try {
      await apiJson(`/inbox/${b.dataset.inboxCreate}:resolve`, {
        method: 'POST',
        body: { action: 'create_person', displayName: b.dataset.inboxName, tier: 'crystal' },
      });
      await refreshBootstrap({ keepPersonId: false });
      renderDirectorySheet();
      showToast(`已为「${b.dataset.inboxName}」建页并归档`);
    } catch (err) { showToast(err.message || '建页失败'); }
  }));

  const form = body.querySelector('#newPersonForm');
  if (form) {
    let chosenTier = 'gold';
    form.querySelector('#btnCancelNewP').addEventListener('click', () => { state.showNewPersonForm = false; renderDirectorySheet(); });
    form.querySelectorAll('[data-new-tier]').forEach((tb) => tb.addEventListener('click', () => {
      chosenTier = tb.dataset.newTier;
      form.querySelectorAll('[data-new-tier]').forEach((x) => x.classList.toggle('is-active', x === tb));
    }));
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const nm = form.querySelector('#newPName').value.trim();
      const rl = form.querySelector('#newPRole').value.trim();
      const mm = form.querySelector('#newPMemo').value.trim();
      if (!nm) return;
      try {
        const created = await apiJson('/persons', {
          method: 'POST',
          body: { nameCn: nm, roleTag: rl || '手帐人物', shortMemo: mm, tier: chosenTier },
        });
        state.showNewPersonForm = false;
        await refreshBootstrap({ keepPersonId: false });
        if (created?.id) selectPerson(created.id);
        closeAllSheets();
        showToast(`已为「${nm}」建页并写入加密库`);
      } catch (err) {
        showToast(err.message || '新建人物失败');
      }
    });
  }
}

/* Sheet 3: 轻声记一笔 (Voice / WeChat Chat / Photo Upload / Care Reminder) */
function startCaptureWave() {
  const cv = document.getElementById('capWaveCv');
  if (!cv) return;
  const ctx = cv.getContext('2d');
  let phase = 0;
  const draw = () => {
    const W = cv.width, H = cv.height;
    ctx.clearRect(0, 0, W, H);
    const bars = 46, gap = W / bars;
    const warm = getComputedStyle(document.documentElement).getPropertyValue('--real-warm').trim() || '#E5BC8B';
    const p = currentPerson();
    const isLight = !p || p.tier === 'silver' || !p.mainPhotoUrl;
    for (let i = 0; i < bars; i++) {
      const amp = 0.25 + 0.65 * Math.abs(Math.sin(phase + i * 0.34) * Math.cos(phase * 0.5 + i * 0.17));
      const bh = Math.max(5, amp * (H - 12));
      ctx.fillStyle = i % 4 === 0 ? warm : (isLight ? 'rgba(32,52,74,0.28)' : 'rgba(255,250,242,0.38)');
      ctx.beginPath();
      ctx.roundRect(i * gap + 2, (H - bh) / 2, Math.max(3, gap - 5), bh, 3);
      ctx.fill();
    }
    phase += 0.08;
    state.capWaveRaf = requestAnimationFrame(draw);
  };
  state.capWaveRaf = requestAnimationFrame(draw);
}

function updateCapturePreview() {
  const p = currentPerson();
  if (!p) return;
  const txt = (document.getElementById('capTextarea').value || '').trim();
  const hits = state.people.filter((x) => txt && txt.includes(x.name));
  const pv = document.getElementById('capAiRoutePreview');
  const drop = document.getElementById('capPhotoDrop');
  drop.classList.toggle('is-show', state.capMode === 'photo');

  const modeLbl =
    state.capMode === 'voice' ? '语音声囊'
      : state.capMode === 'wechat' ? '微信拆解单卡'
        : state.capMode === 'photo' ? '相片留影卡'
          : '记挂提醒';
  if (state.capMode !== 'photo' && hits.length >= 2) {
    pv.textContent = `多人自动拆入 → 「${hits.map((h) => h.name).join('」+「')}」`;
  } else {
    const t = hits[0] || p;
    pv.textContent = t.tier === 'silver' ? `智能路由 →「${t.name}」· 银箭长虹玻璃留痕` : `智能拆解 →「${t.name}」· 自动生成${modeLbl}`;
  }
}

function openCaptureSheet() {
  const p = currentPerson();
  if (!p) return;
  document.getElementById('capSheetTitle').textContent = `随口记一笔 · ${p.name}`;
  document.getElementById('btnCapSaveDirect').textContent = `存入「${p.name}」`;
  updateCapturePreview();
  openSheet('captureSheet');
  startCaptureWave();
}

async function commitSmartCapture(text, autoMulti) {
  const p = currentPerson();
  if (!p) return;
  const raw = (text || '').trim();
  const fileInput = document.getElementById('capPhotoFile');
  const chosenFile = fileInput && fileInput.files && fileInput.files[0];

  if (state.capMode === 'photo') {
    if (!chosenFile) {
      showToast('请先挑选一张照片');
      return;
    }
    const fd = new FormData();
    fd.append('photo', chosenFile);
    fd.append('kind', 'photo');
    fd.append('title', raw.slice(0, 18) || '生活留影');
    fd.append('body', raw || '随手拍下的一帧生活瞬间。');
    fd.append('cardMeta', JSON.stringify({ sceneLabel: raw.slice(0, 14) || '生活留影' }));
    try {
      await apiJson(`/persons/${p.id}/materials`, { method: 'POST', body: fd });
      fileInput.value = '';
      document.getElementById('capPhotoName').textContent = '选择一张生活或人像照片（自动剥离 EXIF 并提取色域）';
      document.getElementById('capTextarea').value = '';
      closeAllSheets();
      await refreshBootstrap({ keepPersonId: true });
      showToast('✦ 已剥离 EXIF 并加密归档相片卡');
    } catch (err) {
      showToast(err.message || '相片上传失败');
    }
    return;
  }

  if (!raw) {
    showToast('先说一句或粘贴一段对话再存入');
    return;
  }

  if (state.capMode === 'care') {
    try {
      const targetDate = new Date(Date.now() + 86400000 * 3).toISOString().slice(0, 10);
      await apiJson('/reminders', {
        method: 'POST',
        body: {
          personId: p.id,
          title: raw.slice(0, 120),
          rule: 'once',
          onceDate: targetDate,
          leadDays: [1, 0],
        },
      });
      await apiJson(`/persons/${p.id}/materials`, {
        method: 'POST',
        body: { kind: 'note', title: '记挂待办', body: raw },
      });
      document.getElementById('capTextarea').value = '';
      closeAllSheets();
      await refreshBootstrap({ keepPersonId: true });
      showToast(`✦ 已写入「${p.name}」近期记挂提醒带`);
    } catch (err) {
      showToast(err.message || '创建记挂失败');
    }
    return;
  }

  const source = state.capMode === 'wechat' ? 'chat' : 'text';
  const routeMode = autoMulti ? 'global' : 'current';
  try {
    const res = await apiJson('/captures?sync=true', {
      method: 'POST',
      body: {
        source,
        routeMode,
        activePersonId: p.id,
        text: raw,
      },
    });
    document.getElementById('capTextarea').value = '';
    closeAllSheets();
    closeVoiceArc();
    showDispatchReceipt(res);
    const receiptEl = document.getElementById('dispatchReceipt');
    if (receiptEl) {
      clearTimeout(receiptEl._refreshT);
      receiptEl._refreshT = setTimeout(() => {
        refreshBootstrap({ keepPersonId: true }).catch(() => {});
      }, 250);
    } else {
      refreshBootstrap({ keepPersonId: true }).catch(() => {});
    }
  } catch (err) {
    showToast(err.message || '归档失败');
  }
}

function showDispatchReceipt(captureResp) {
  const box = document.getElementById('dispatchReceipt');
  if (captureResp && captureResp.crisisCard) {
    const cc = captureResp.crisisCard;
    const hotlines = (cc.hotlines || []).map((h) => `${h.name || '热线'} ${h.phone || h.number || ''}`).join(' · ');
    showToast(`${cc.title || '先让我陪着你'}：${hotlines || '12356 / 110'}`);
    return;
  }
  const splits = (captureResp && Array.isArray(captureResp.splits)) ? captureResp.splits : [];
  if (!splits.length) {
    showToast('已记录入待整理收件箱');
    return;
  }
  const captureId = captureResp.captureId || captureResp.jobId;
  box.innerHTML = `
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;">
      <strong style="font-size:13px;">✓ 已落墨拆入 ${splits.length} 条手帐</strong>
      ${captureId ? `<button class="s-pill" id="btnUndoDispatch" type="button" style="height:24px;font-size:11px;">撤销 5s</button>` : ''}
    </div>
    ${splits.map((sp) => {
      const target = state.people.find((x) => x.id === sp.personId);
      const tLabel = target ? TIER_META[target.tier]?.label || '' : '';
      const snippet = sp.body || sp.summary || '已加密归档';
      return `
        <div class="receipt-row">
          <div style="min-width:0;flex:1;">
            <strong style="margin-right:6px;">${escapeHtml(sp.personName || target?.name || '手帐')} ${tLabel ? `· ${tLabel}` : ''}</strong>
            <div style="font-size:11.5px;opacity:0.74;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${escapeHtml(snippet)}</div>
          </div>
          ${sp.personId ? `<button class="s-pill" data-jump-person="${sp.personId}" type="button" style="height:24px;font-size:11px;">去看 ›</button>` : ''}
        </div>
      `;
    }).join('')}
  `;
  box.classList.add('is-open');

  const undoBtn = box.querySelector('#btnUndoDispatch');
  clearInterval(box._undoIv);
  if (undoBtn && captureId) {
    const deadlineMs = captureResp.undoDeadline
      ? Date.parse(captureResp.undoDeadline)
      : Date.now() + 4800;
    const syncCountdown = () => {
      const remMs = deadlineMs - Date.now();
      if (remMs <= 250) {
        clearInterval(box._undoIv);
        undoBtn.remove();
        return;
      }
      undoBtn.textContent = `撤销 ${Math.max(1, Math.ceil(remMs / 1000))}s`;
    };
    syncCountdown();
    box._undoIv = setInterval(syncCountdown, 400);
    let undoing = false;
    undoBtn.addEventListener('click', async () => {
      if (undoing) return;
      undoing = true;
      undoBtn.disabled = true;
      clearInterval(box._undoIv);
      try {
        await apiJson(`/captures/${captureId}:undo`, { method: 'POST', body: {} });
        box.classList.remove('is-open');
        await refreshBootstrap({ keepPersonId: true });
        showToast('已撤回刚刚的分拣记录');
      } catch (err) {
        undoBtn.remove();
        showToast(err.message || '撤销窗口已过');
      }
    });
  }
  box.querySelectorAll('[data-jump-person]').forEach((b) => b.addEventListener('click', () => {
    clearInterval(box._undoIv);
    box.classList.remove('is-open');
    selectPerson(b.dataset.jumpPerson);
  }));
  clearTimeout(box._t);
  box._t = setTimeout(() => {
    clearInterval(box._undoIv);
    box.classList.remove('is-open');
  }, 7500);
}

/* Split-Arc Voice Stage (`断弧语音`) */
function openVoiceArc() {
  const p = currentPerson();
  if (!p) return;
  closeAllSheets();
  document.getElementById('routeCurrentLabel').textContent = `→ 默认此人（${p.name}）`;
  document.getElementById('voiceArcStage').classList.add('is-open');
}
function closeVoiceArc() {
  document.getElementById('voiceArcStage').classList.remove('is-open');
}

/* Sheet 4: 设置 · 生物锁 · 真实 PIPL 数据主权导出 */
function openSettingsSheet() {
  const body = document.getElementById('settingsSheetBody');
  const email = state.session?.email || '当前加密会话';
  body.innerHTML = `
    <div class="s-card">
      <div class="s-label"><span>光影与琉璃氛围</span><span>Lumina 实时生效</span></div>
      <div class="s-pill-row">
        <button class="s-pill ${state.lighting === 'daylight' ? 'is-active' : ''}" data-set-light="daylight" type="button">晨光通透（原图不虚化）</button>
        <button class="s-pill ${state.lighting === 'evening' ? 'is-active' : ''}" data-set-light="evening" type="button">暮色深琉璃</button>
      </div>
    </div>

    <div class="s-card">
      <div class="s-label"><span>隐私与账户密钥</span><span>独立密钥加密隔离</span></div>
      <div style="font-size:12.5px;opacity:0.78;">已登录：${escapeHtml(email)}</div>
      <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;">
        <div>
          <div style="font-size:13.5px;font-weight:600;">面容 ID 锁屏保护</div>
          <div style="font-size:11.5px;opacity:0.68;margin-top:2px;">切出后台立即拉起毛玻璃隐私幕布</div>
        </div>
        <button class="s-pill ${state.faceIdLock ? 'is-active' : ''}" id="btnToggleFaceId" type="button">${state.faceIdLock ? '已开启' : '已关闭'}</button>
      </div>
      <div style="display:flex;gap:8px;margin-top:4px;">
        <button class="s-pill" id="btnSettingsLogout" type="button">退出当前账号</button>
      </div>
    </div>

    <div class="s-card">
      <div class="s-label"><span>数据主权（《个保法》第 45/47 条）</span><span>GB 45438-2025</span></div>
      <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;">
        <div>
          <div style="font-size:13px;font-weight:600;">导出全部加密手帐（.zip + SHA-256）</div>
          <div style="font-size:11.5px;opacity:0.68;">含全部原文、照片、玉签与完整校验清单</div>
        </div>
        <button class="s-pill is-active" id="btnExportData" type="button">实时导出</button>
      </div>
      <div id="exportResultLine" style="font-size:11.5px;opacity:0.74;word-break:break-all;"></div>
    </div>

    <div class="s-card">
      <div class="s-label"><span>双层生命安全保护通道</span><span>随时守候</span></div>
      <div style="font-size:12px;line-height:1.62;opacity:0.80;">任何箭矢层级下，若识别到极端情绪或人身安全危机，立即固定提供 12356 / 110 / 12338 求助通道，永不诱导病态情感依赖。</div>
    </div>
  `;

  body.querySelectorAll('[data-set-light]').forEach((b) => b.addEventListener('click', () => {
    state.lighting = b.dataset.setLight;
    document.documentElement.setAttribute('data-lighting', state.lighting);
    openSettingsSheet();
  }));
  body.querySelector('#btnToggleFaceId').addEventListener('click', () => { state.faceIdLock = !state.faceIdLock; openSettingsSheet(); });
  const lo = body.querySelector('#btnSettingsLogout');
  if (lo) {
    lo.addEventListener('click', async () => {
      try {
        await apiJson('/auth/logout', { method: 'POST', body: { refreshToken: state.session?.refreshToken } });
      } catch (_) {}
      saveSession(null);
      state.people = [];
      state.personId = null;
      closeAllSheets();
      openAuthGate();
    });
  }
  body.querySelector('#btnExportData').addEventListener('click', () => handleLivePiplExport(body.querySelector('#exportResultLine')));
  openSheet('settingsSheet');
}

async function handleLivePiplExport(statusEl) {
  if (!state.session?.email) return;
  if (statusEl) statusEl.textContent = '正在校验二次授权并打包解密归档…';
  try {
    const ch = await apiJson('/auth/email/otp:request', {
      method: 'POST',
      body: { email: state.session.email, purpose: 'reauth' },
    });
    if (!ch || !ch.devOtpCode) {
      if (statusEl) statusEl.textContent = '请查收邮箱二次校验码后在账号安全页导出。';
      return;
    }
    const verified = await apiJson('/auth/email/otp:verify', {
      method: 'POST',
      attachBearer: true,
      body: { challengeId: ch.challengeId, email: state.session.email, code: ch.devOtpCode },
    });
    const exp = await apiJson('/me/exports', {
      method: 'POST',
      headers: { 'X-Reauth-Token': verified.reauthToken },
      body: { asyncMode: false },
    });
    let kbText = '';
    if (exp.downloadPath) {
      const dlResp = await fetch(exp.downloadPath, {
        headers: { Authorization: `Bearer ${state.session.accessToken}` },
      });
      if (dlResp.ok) {
        const blob = await dlResp.blob();
        kbText = ` · ${Math.max(1, Math.round(blob.size / 1024))} KB`;
        const isNativeIos = Boolean(window.__MW_NATIVE_IOS__ || window.__MW_IOS_NATIVE__);
        if (!isNativeIos) {
          const url = URL.createObjectURL(blob);
          const a = document.createElement('a');
          a.href = url;
          a.download = `mindweave_export_${String(exp.exportId).slice(0, 8)}.zip`;
          document.body.appendChild(a);
          a.click();
          a.remove();
          setTimeout(() => URL.revokeObjectURL(url), 5000);
        }
      }
    }
    if (statusEl) statusEl.textContent = `✓ 导出包就绪${kbText} · SHA-256: ${(exp.sha256Hex || '').slice(0, 20)}…`;
    showToast('✓ 已生成完整 GB 45438-2025 数据主权归档包');
  } catch (err) {
    if (statusEl) statusEl.textContent = `导出暂未完成：${err.message}`;
    showToast(err.message || '导出失败');
  }
}

/* Sheet 5: 农历/公历三列滚轮核对 -> Live Reminder + Draft Patch */
function openDateWheelSheet(draftRecId) {
  if (draftRecId !== undefined) state.wheelDraftRecId = draftRecId;
  const p = currentPerson();
  if (!p) return;
  const draft = state.wheelDraftRecId ? p.records.find((r) => r.id === state.wheelDraftRecId) : null;
  const body = document.getElementById('dateWheelBody');
  const years = [2026, 2027, 2028], months = [11, 12, 1], days = [15, 16, 17, 28];

  body.innerHTML = `
    <div class="s-card">
      <div class="s-label"><span>记挂原话</span><span>${escapeHtml(draft?.lunar_label || '农历/公历双历')}</span></div>
      <div style="font-family:var(--font-serif);font-size:14px;line-height:1.62;">${escapeHtml(draft?.text || `为「${p.name}」核对一个需要提前提醒的温暖日子。`)}</div>
      <input class="spot-input" id="wheelTitleInput" type="text" value="${escapeHtml(draft?.card_title || draft?.text?.slice(0, 28) || `给${p.name}准备心意`)}" style="padding:8px 11px;border-radius:11px;background:rgba(255,255,255,0.08);border:0.75px solid rgba(255,255,255,0.16);" />
    </div>
    <div class="s-card">
      <div class="s-label"><span>对应公历日期核对</span><span>${state.wheelYear}年${state.wheelMonth}月${state.wheelDay}日</span></div>
      <div class="wheel-grid">
        <div class="wheel-col">${years.map((y) => `<button class="wheel-opt ${y === state.wheelYear ? 'is-sel' : ''}" data-wy="${y}" type="button">${y}年</button>`).join('')}</div>
        <div class="wheel-col">${months.map((m) => `<button class="wheel-opt ${m === state.wheelMonth ? 'is-sel' : ''}" data-wm="${m}" type="button">${m}月</button>`).join('')}</div>
        <div class="wheel-col">${days.map((d) => `<button class="wheel-opt ${d === state.wheelDay ? 'is-sel' : ''}" data-wd="${d}" type="button">${d}日</button>`).join('')}</div>
      </div>
      <button class="s-btn-primary" id="btnConfirmWheel" type="button" style="margin-top:4px;">确认落墨并提前 1 天提醒</button>
    </div>
  `;

  body.querySelectorAll('[data-wy]').forEach((b) => b.addEventListener('click', () => { state.wheelYear = Number(b.dataset.wy); openDateWheelSheet(); }));
  body.querySelectorAll('[data-wm]').forEach((b) => b.addEventListener('click', () => { state.wheelMonth = Number(b.dataset.wm); openDateWheelSheet(); }));
  body.querySelectorAll('[data-wd]').forEach((b) => b.addEventListener('click', () => { state.wheelDay = Number(b.dataset.wd); openDateWheelSheet(); }));
  body.querySelector('#btnConfirmWheel').addEventListener('click', async () => {
    const title = (body.querySelector('#wheelTitleInput').value || '').trim() || `记挂「${p.name}」`;
    const onceDate = `${state.wheelYear}-${String(state.wheelMonth).padStart(2, '0')}-${String(state.wheelDay).padStart(2, '0')}`;
    try {
      await apiJson('/reminders', {
        method: 'POST',
        body: { personId: p.id, title, rule: 'once', onceDate, leadDays: [1, 0] },
      });
      if (draft) {
        await apiJson(`/materials/${draft.id}`, {
          method: 'PATCH',
          headers: { 'If-Match': `"${draft.version}"` },
          body: {
            cardMeta: {
              ...(draft.cardMeta || {}),
              lunarHint: `✓ 已落墨 ${state.wheelYear}·${state.wheelMonth}·${state.wheelDay}（提前 1 天）`,
            },
          },
        });
      }
      closeAllSheets();
      await refreshBootstrap({ keepPersonId: true });
      showToast(`✦ 已落墨 ${state.wheelYear}年${state.wheelMonth}月${state.wheelDay}日 并写入提醒`);
    } catch (err) {
      showToast(err.message || '写入提醒失败');
    }
  });
  openSheet('dateWheelSheet');
}

/* ============================================================================
   9. SPOTLIGHT ⌘K & APPLE PHOTOS DARKROOM LIGHTBOX
   ============================================================================ */
function openSpotlight() {
  closeAllSheets();
  document.getElementById('spotlightOverlay').classList.add('is-open');
  renderSpotlight();
  setTimeout(() => document.getElementById('spotInput').focus(), 40);
}
function closeSpotlight() {
  document.getElementById('spotlightOverlay').classList.remove('is-open');
  document.getElementById('spotInput').value = '';
}
function hlText(txt, q) {
  const safe = escapeHtml(txt || '');
  if (!q) return safe;
  const esc = q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return safe.replace(new RegExp(esc, 'gi'), (m) => `<mark class="spot-mark">${m}</mark>`);
}

function renderSpotlight() {
  const q = (document.getElementById('spotInput').value || '').trim();
  const ql = q.toLowerCase();
  const p = currentPerson();
  if (!p) return;
  const items = [];

  if (q) {
    state.people.filter((x) => x.id !== p.id && (x.name + x.role).toLowerCase().includes(ql)).forEach((o) => {
      items.push({ icon: '👤', title: `切换至「${o.name}」`, sub: o.role, go: () => { selectPerson(o.id); closeSpotlight(); } });
    });
  }
  if (state.spotFilter === 'all' || state.spotFilter === 'fact') {
    (p.facts || []).forEach((f) => {
      if (!ql || (f.kind + f.text).toLowerCase().includes(ql)) {
        items.push({ icon: '✦', title: `${f.kind} · ${f.text}`, sub: `${p.name} · 人物玉签`, go: () => { closeSpotlight(); openDossierSheet(); } });
      }
    });
  }
  p.records.forEach((r) => {
    const cat = r.type === 'shield-log' ? 'chat' : r.type;
    if (state.spotFilter !== 'all' && state.spotFilter !== cat) return;
    const body = r.caption || (r.segments && r.segments.join('')) || r.summary || r.text || '';
    const hay = `${r.card_title || ''} ${r.tag || ''} ${r.date || ''} ${body}`.toLowerCase();
    if (!ql || hay.includes(ql)) {
      const icon = r.type === 'voice' ? '🎙' : r.type === 'photo' ? '🖼' : r.type === 'chat' ? '💬' : r.type === 'shield-log' ? '🛡' : r.type === 'draft' ? '✎' : '✦';
      items.push({
        icon,
        title: r.card_title || r.tag || r.date,
        sub: `${r.date} · ${body.slice(0, 40)}`,
        go: () => {
          closeSpotlight();
          if (p.tier !== 'silver' && r.type === 'photo' && r.img) openLightboxById(r.id);
          else jumpToRecord(r.id);
        },
      });
    }
  });

  const box = document.getElementById('spotResults');
  if (!items.length) {
    box.innerHTML = `<div style="padding:16px;text-align:center;font-size:12.5px;opacity:0.62;">未找到含「${escapeHtml(q)}」的记忆</div>`;
    return;
  }
  box.innerHTML = items.slice(0, 8).map((it, i) => `
    <button class="spot-item" data-sp-i="${i}" type="button">
      <span style="width:26px;height:26px;border-radius:8px;background:rgba(255,255,255,0.12);display:inline-flex;align-items:center;justify-content:center;font-size:12px;flex-shrink:0;">${it.icon}</span>
      <span style="flex:1;min-width:0;">
        <div style="font-size:13px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${hlText(it.title, q)}</div>
        <div style="font-size:11.5px;opacity:0.68;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${hlText(it.sub, q)}</div>
      </span>
      <span style="opacity:0.45;font-size:12px;">↗</span>
    </button>
  `).join('');
  box.querySelectorAll('[data-sp-i]').forEach((b) => b.addEventListener('click', () => items[Number(b.dataset.spI)].go()));
}

function openLightboxById(recId) {
  const p = currentPerson();
  if (!p || p.tier === 'silver') return;
  const photos = p.records.filter((r) => r.type === 'photo' && r.img);
  if (!photos.length) return;
  state.lbPhotos = photos;
  state.lbIndex = Math.max(0, photos.findIndex((x) => x.id === recId));
  renderLightbox();
  document.getElementById('lightboxOverlay').classList.add('is-open');
}

function renderLightbox() {
  const rec = state.lbPhotos[state.lbIndex];
  const p = currentPerson();
  if (!rec || !p) return;
  document.getElementById('lbCounter').textContent = `${String(state.lbIndex + 1).padStart(2, '0')} / ${String(state.lbPhotos.length).padStart(2, '0')} · ${rec.date} · ${rec.tag}`;
  const pal = (rec.palette || ['#D8C2A0', '#8B7D6B', '#463D34']).slice(0, 3);
  document.getElementById('lbPigments').innerHTML = pal.map((c) => `<i style="width:7px;height:7px;border-radius:50%;background:${c};display:block;"></i>`).join('');
  document.getElementById('lbMainImg').src = rec.img;
  document.getElementById('lbCaption').textContent = rec.caption || '';
  document.getElementById('btnLbFav').textContent = rec.fav ? '★ 已珍藏' : '☆ 珍藏';
  document.getElementById('btnLbSetBg').textContent = p.activeHeroMaterialId === rec.id ? '✓ 已作背景' : '✦ 映作背景';
  const th = document.getElementById('lbThumbs');
  th.innerHTML = state.lbPhotos.map((ph, i) => `<button class="lb-thumb-btn ${i === state.lbIndex ? 'is-active' : ''}" data-lbi="${i}" type="button"><img src="${ph.img}" alt="" /></button>`).join('');
  th.querySelectorAll('[data-lbi]').forEach((b) => b.addEventListener('click', () => { state.lbIndex = Number(b.dataset.lbi); renderLightbox(); }));
}

/* ============================================================================
   10. 1-CLICK LIVE SEEDING INTO POSTGRESQL (8 AUTHENTIC EVERYDAY PERSONAS)
   ============================================================================ */
async function seedDemoPackIntoBackend() {
  if (state.seeding) return;
  state.seeding = true;
  const btn = document.getElementById('btnLoadDemoPack');
  const track = document.getElementById('seedProgressTrack');
  const fill = document.getElementById('seedProgressFill');
  const lbl = document.getElementById('seedProgressLabel');
  if (btn) btn.disabled = true;
  track.classList.add('is-active');

  try {
    lbl.textContent = '正在载入示范手帐清单…';
    const bundleResp = await fetch('/assets/v16/seed_bundle.json');
    const bundle = await bundleResp.json();
    const personas = bundle.personas || [];

    for (let i = 0; i < personas.length; i++) {
      const src = personas[i];
      const pct = Math.round(((i + 0.2) / personas.length) * 100);
      fill.style.width = `${pct}%`;
      lbl.textContent = `正在加密写入「${src.name}」(${i + 1}/${personas.length})…`;

      const heroPrint = (src.stage_prints || []).find((pr) => pr.slug === src.hero_key) || (src.stage_prints && src.stage_prints[0]) || null;
      const personDto = await apiJson('/persons', {
        method: 'POST',
        body: {
          nameCn: src.name,
          roleTag: (src.role || '').split('·').pop().trim() || '手帐人物',
          contextSub: (src.stage_cue || '').slice(0, 120),
          shortMemo: (src.persona_description?.summary || '').slice(0, 240),
          traits: (src.persona_description?.facts || []).slice(0, 4).map((f) => String(f.text || '').slice(0, 180)),
          tier: src.tier || 'gold',
          visualTokens: {
            faceZone: heroPrint?.faceZone || 'center',
            deskFocus: heroPrint?.deskFocus || '54% 24%',
            phoneFocus: heroPrint?.phoneFocus || '54% 24%',
            cupidPromptsCsv: (src.cupid?.prompts || []).join('|').slice(0, 240),
          },
        },
      });
      const pid = personDto.id;

      // Upload authentic hero portrait photo (strictly skipped on Silver Shield!)
      if (src.tier !== 'silver' && src.hero_key) {
        const imgResp = await fetch(`/assets/v16/${src.hero_key}.webp`);
        if (imgResp.ok) {
          const blob = await imgResp.blob();
          const fd = new FormData();
          fd.append('file', blob, `${src.hero_key}.webp`);
          const afterUpload = await apiJson(`/persons/${pid}/photos/main`, { method: 'POST', body: fd });
          // Preserve curated portrait framing alongside server-extracted pigments
          if (heroPrint && afterUpload) {
            const mergedTokens = {
              ...(afterUpload.visualTokens || {}),
              faceZone: heroPrint.faceZone || afterUpload.visualTokens?.faceZone || 'center',
              deskFocus: heroPrint.deskFocus || afterUpload.visualTokens?.deskFocus || '54% 24%',
              phoneFocus: heroPrint.phoneFocus || afterUpload.visualTokens?.phoneFocus || '54% 24%',
            };
            // Keep only scalar tokens
            Object.keys(mergedTokens).forEach((k) => {
              const v = mergedTokens[k];
              if (v !== null && typeof v === 'object') delete mergedTokens[k];
            });
            await apiJson(`/persons/${pid}`, {
              method: 'PATCH',
              headers: { 'If-Match': `"${afterUpload.version}"` },
              body: { visualTokens: mergedTokens },
            });
          }
        }
      }

      // Seed facts
      for (const f of (src.persona_description?.facts || []).slice(0, 4)) {
        const mapped = LABEL_TO_FACT_TYPE[f.kind] || { type: 'preference', polarity: 'positive' };
        await apiJson(`/persons/${pid}/facts`, {
          method: 'POST',
          body: { type: mapped.type, polarity: mapped.polarity, content: f.text },
        });
      }

      // Seed voice memos
      for (const v of (src.voice_memos || [])) {
        await apiJson(`/persons/${pid}/materials`, {
          method: 'POST',
          body: {
            kind: 'voice',
            title: v.card_title || v.tag || '语音速记',
            body: (v.segments || []).join(''),
            isFavorite: Boolean(v.fav),
            cardMeta: {
              dateLabel: v.date,
              timeLabel: v.time || '',
              tag: v.tag || '语音速记',
              duration: v.duration || 16,
              segments: v.segments || [],
              ring: v.ring || 1,
              angle: v.angle || 54,
            },
          },
        });
      }

      // Seed decomposed WeChat chats
      for (const c of (src.chat_records || [])) {
        const msgs = (c.messages || []).slice(0, 4).map((m) => ({
          from: m.sender || m.from || src.name,
          sender: m.is_target === false || m.sender === '我' ? 'me' : 'them',
          text: m.text,
        }));
        await apiJson(`/persons/${pid}/materials`, {
          method: 'POST',
          body: {
            kind: 'chat',
            title: c.card_title || c.tag || '微信片段',
            body: c.distilled || c.summary || msgs.map((m) => `${m.from}：${m.text}`).join('\n'),
            snippet: (c.takeaway || c.summary || '').slice(0, 120),
            isFavorite: Boolean(c.fav),
            cardMeta: {
              dateLabel: c.date,
              timeLabel: c.time || '',
              tag: c.tag || '微信片段',
              summary: c.takeaway || c.summary || '',
              messages: msgs,
              ring: c.ring || 1,
              angle: c.angle || 168,
            },
          },
        });
      }

      // Seed real photo cards (up to 3 per person for crisp speed & rich gallery)
      if (src.tier !== 'silver') {
        for (const ph of (src.photo_records || []).slice(0, 3)) {
          const prResp = await fetch(`/assets/v16/${ph.media_slug}.webp`);
          if (!prResp.ok) continue;
          const pBlob = await prResp.blob();
          const fd = new FormData();
          fd.append('photo', pBlob, `${ph.media_slug}.webp`);
          fd.append('kind', 'photo');
          fd.append('title', ph.tag || '生活留影');
          fd.append('body', ph.caption || '');
          fd.append('isFavorite', ph.fav ? 'true' : 'false');
          fd.append('cardMeta', JSON.stringify({
            dateLabel: ph.date,
            timeLabel: ph.time || '',
            tag: ph.tag || '相片',
            sceneLabel: ph.scene_label || ph.tag || '',
            palette: ph.palette || [],
            ring: ph.ring || 1,
            angle: ph.angle || 132,
          }));
          await apiJson(`/persons/${pid}/materials`, { method: 'POST', body: fd });
        }
      }

      // Seed silver shield logs
      for (const sl of (src.shield_logs || [])) {
        await apiJson(`/persons/${pid}/materials`, {
          method: 'POST',
          body: {
            kind: 'note',
            title: sl.tag || '边界留痕',
            body: sl.text || '',
            isFavorite: Boolean(sl.fav),
            cardMeta: { dateLabel: sl.date, timeLabel: sl.time || '', tag: sl.tag || '边界留痕', ring: sl.ring || 1, angle: sl.angle || 45 },
          },
        });
      }

      // Seed draft slip
      if (src.draft_slip) {
        const dr = src.draft_slip;
        await apiJson(`/persons/${pid}/materials`, {
          method: 'POST',
          body: {
            kind: 'draft',
            title: dr.tag || '待核对日期',
            body: dr.text || '',
            isFavorite: Boolean(dr.fav),
            cardMeta: {
              dateLabel: dr.date,
              tag: dr.tag || '待核对日期',
              lunarLabel: dr.lunar_label || '',
              lunarHint: dr.lunar_hint || '12月16日 ✎ · 轻点核对',
              ring: dr.ring || 2,
              angle: dr.angle || 212,
            },
          },
        });
      }

      // Seed first care reminder
      const firstCare = (src.care_days && src.care_days[0]) || null;
      if (firstCare) {
        const soonDate = new Date(Date.now() + 86400000 * (4 + i)).toISOString().slice(0, 10);
        await apiJson('/reminders', {
          method: 'POST',
          body: { personId: pid, title: firstCare.title, rule: 'once', onceDate: soonDate, leadDays: [1, 0] },
        });
      }
    }

    fill.style.width = '100%';
    lbl.textContent = '✓ 全部 8 位示范人物已加密备妥，正在开启手帐…';
    await refreshBootstrap({ keepPersonId: false });
    showToast('✦ 已载入 8 位心络日常示范人物');
  } catch (err) {
    lbl.textContent = `载入中断：${err.message}`;
    showToast(err.message || '载入示范集失败');
  } finally {
    state.seeding = false;
    if (btn) btn.disabled = false;
  }
}

/* ============================================================================
   11. AUTH GATE (DEV 1-TAP + MANUAL EMAIL OTP)
   ============================================================================ */
function openAuthGate() {
  document.getElementById('authGateOverlay').classList.add('is-open');
}
function closeAuthGate() {
  document.getElementById('authGateOverlay').classList.remove('is-open');
}

async function handleQuickGuestLogin() {
  const btn = document.getElementById('btnQuickGuestLogin');
  const hint = document.getElementById('authHint');
  btn.disabled = true;
  hint.textContent = '正在建立专属加密会话…';
  try {
    const email = getOrCreateGuestEmail();
    const ch = await apiJson('/auth/email/otp:request', {
      method: 'POST',
      body: { email, purpose: 'login' },
    });
    if (!ch || !ch.devOtpCode) {
      document.getElementById('authEmailInput').value = email;
      hint.textContent = '请输入邮箱收到的 6 位验证码登录';
      document.getElementById('authOtpRow').style.display = 'flex';
      state.otpChallenge = { challengeId: ch.challengeId, email };
      return;
    }
    const verified = await apiJson('/auth/email/otp:verify', {
      method: 'POST',
      body: { challengeId: ch.challengeId, email, code: ch.devOtpCode },
    });
    saveSession({
      userId: verified.userId,
      email,
      accessToken: verified.accessToken,
      refreshToken: verified.refreshToken,
    });
    closeAuthGate();
    await refreshBootstrap({ keepPersonId: false });
  } catch (err) {
    hint.textContent = err.message || '登录稍忙，请重试';
  } finally {
    btn.disabled = false;
  }
}

async function handleManualRequestOtp() {
  const email = (document.getElementById('authEmailInput').value || '').trim().toLowerCase();
  const hint = document.getElementById('authHint');
  if (!email || !email.includes('@')) {
    hint.textContent = '请输入有效邮箱地址';
    return;
  }
  hint.textContent = '正在发送验证码…';
  try {
    const ch = await apiJson('/auth/email/otp:request', {
      method: 'POST',
      body: { email, purpose: 'login' },
    });
    state.otpChallenge = { challengeId: ch.challengeId, email };
    document.getElementById('authOtpRow').style.display = 'flex';
    if (ch.devOtpCode) {
      document.getElementById('authOtpInput').value = ch.devOtpCode;
      hint.textContent = '开发验证码已自动填入，轻点登录即可';
    } else {
      hint.textContent = '验证码已发送，请查收邮箱';
    }
    document.getElementById('authOtpInput').focus();
  } catch (err) {
    hint.textContent = err.message || '发送失败';
  }
}

async function handleManualVerifyOtp(e) {
  e.preventDefault();
  const hint = document.getElementById('authHint');
  const code = (document.getElementById('authOtpInput').value || '').trim();
  if (!state.otpChallenge || !code) {
    await handleManualRequestOtp();
    return;
  }
  hint.textContent = '正在校验并解密个人档案…';
  try {
    const verified = await apiJson('/auth/email/otp:verify', {
      method: 'POST',
      body: { challengeId: state.otpChallenge.challengeId, email: state.otpChallenge.email, code },
    });
    saveSession({
      userId: verified.userId,
      email: state.otpChallenge.email,
      accessToken: verified.accessToken,
      refreshToken: verified.refreshToken,
    });
    closeAuthGate();
    await refreshBootstrap({ keepPersonId: false });
  } catch (err) {
    hint.textContent = err.message || '验证码无效或已过期';
  }
}

/* ============================================================================
   12. EVENT WIRING & BOOTSTRAP
   ============================================================================ */
function wireEvents() {
  if (window.__MW_NATIVE_IOS__ || window.__MW_IOS_NATIVE__) {
    document.documentElement.classList.add('is-native-ios');
    document.documentElement.classList.remove('v15-iphone-sim');
  } else if (new URLSearchParams(window.location.search).get('iphone') === '1') {
    document.documentElement.classList.add('v15-iphone-sim');
  }

  // Native iOS WKWebView push bridge (status bar tap-to-top + keyboard frame sync)
  window.__mwNativeEvent = (ev) => {
    if (!ev || typeof ev !== 'object') return;
    if (ev.type === 'statusbar.tap') {
      scrollMainToTop(true);
    } else if (ev.type === 'keyboard.change' && typeof ev.height === 'number') {
      document.documentElement.style.setProperty('--mw-kb-inset', `${Math.max(0, Math.round(ev.height))}px`);
    }
  };

  // Mobile Safari visualViewport keyboard fallback when outside native WKWebView
  if (window.visualViewport && !(window.__MW_NATIVE_IOS__ || window.__MW_IOS_NATIVE__)) {
    const syncVv = () => {
      const kb = Math.max(0, Math.round(window.innerHeight - window.visualViewport.height - window.visualViewport.offsetTop));
      document.documentElement.style.setProperty('--mw-kb-inset', `${kb > 80 ? kb : 0}px`);
    };
    window.visualViewport.addEventListener('resize', syncVv);
  }

  document.getElementById('btnEnterSim').addEventListener('click', () => {
    document.documentElement.classList.add('v15-iphone-sim');
    syncChromaForPerson(currentPerson());
  });
  document.getElementById('btnExitSim').addEventListener('click', () => {
    document.documentElement.classList.remove('v15-iphone-sim');
    syncChromaForPerson(currentPerson());
  });

  // Auth Gate
  document.getElementById('btnQuickGuestLogin').addEventListener('click', handleQuickGuestLogin);
  document.getElementById('btnRequestOtp').addEventListener('click', handleManualRequestOtp);
  document.getElementById('authOtpForm').addEventListener('submit', handleManualVerifyOtp);

  // Empty welcome actions
  document.getElementById('btnLoadDemoPack').addEventListener('click', seedDemoPackIntoBackend);
  document.getElementById('btnWelcomeCreateFirst').addEventListener('click', () => openDirectorySheet(true));

  // Direct hero photo upload input
  document.getElementById('dossierBgUploadInput').addEventListener('change', (e) => {
    const f = e.target.files && e.target.files[0];
    if (f) uploadDirectHeroPhoto(currentPerson(), f);
    e.target.value = '';
  });

  // Top bar
  document.getElementById('btnOpenSettings').addEventListener('click', openSettingsSheet);
  document.getElementById('btnOpenSpotlight').addEventListener('click', openSpotlight);

  const arrowPop = document.getElementById('arrowPopover');
  document.getElementById('btnArrowPin').addEventListener('click', (e) => {
    e.stopPropagation();
    arrowPop.classList.toggle('is-open');
  });
  document.addEventListener('click', () => arrowPop.classList.remove('is-open'));
  document.querySelectorAll('[data-tier-pick]').forEach((b) => b.addEventListener('click', () => {
    switchPersonTier(b.dataset.tierPick);
  }));

  // Masthead & Time Scales
  document.getElementById('btnOpenDossier').addEventListener('click', openDossierSheet);
  document.getElementById('btnCareRibbon').addEventListener('click', openDossierSheet);
  document.querySelectorAll('.scale-tab').forEach((t) => t.addEventListener('click', () => {
    state.scale = t.dataset.scale;
    renderFeed();
  }));
  document.getElementById('btnScaleRightAction').addEventListener('click', () => {
    const p = currentPerson();
    if (!p) return;
    if (p.tier === 'silver') { state.shieldGlobalOpen = !state.shieldGlobalOpen; renderFeed(); }
    else openDossierSheet();
  });

  // Tri-Dock Voice Orb (Hold >=360ms -> Split-Arc / Quick Tap click -> 4-Mode Quick Capture)
  // Using `click` for quick-tap eliminates iOS WebKit ghost-click dismissal on `#sheetBackdrop`!
  const voiceOrb = document.getElementById('dockVoiceOrb');
  let holdTimer = null, didHold = false, startX = 0, startY = 0;
  const clearHold = () => { if (holdTimer) { clearTimeout(holdTimer); holdTimer = null; } };
  voiceOrb.addEventListener('pointerdown', (e) => {
    didHold = false;
    startX = e.clientX || 0;
    startY = e.clientY || 0;
    clearHold();
    holdTimer = setTimeout(() => {
      didHold = true;
      holdTimer = null;
      try { window.webkit?.messageHandlers?.mwHaptic?.postMessage('medium'); } catch (_) {}
      openVoiceArc();
    }, 360);
  });
  voiceOrb.addEventListener('pointermove', (e) => {
    if (holdTimer && Math.hypot((e.clientX || 0) - startX, (e.clientY || 0) - startY) > 14) {
      clearHold();
    }
  });
  voiceOrb.addEventListener('pointerup', clearHold);
  voiceOrb.addEventListener('pointerleave', clearHold);
  voiceOrb.addEventListener('pointercancel', clearHold);
  voiceOrb.addEventListener('contextmenu', (e) => e.preventDefault());
  voiceOrb.addEventListener('click', (e) => {
    clearHold();
    if (didHold) {
      e.preventDefault();
      e.stopPropagation();
      didHold = false;
      return;
    }
    openCaptureSheet();
  });

  // Split-Arc Voice Stage
  document.getElementById('btnLaunchSplitArc').addEventListener('click', openVoiceArc);
  document.getElementById('btnRouteAuto').addEventListener('click', () => {
    state.capMode = 'voice';
    commitSmartCapture(document.getElementById('voiceArcInput').value, true);
  });
  document.getElementById('btnRouteCurrent').addEventListener('click', () => {
    state.capMode = 'voice';
    commitSmartCapture(document.getElementById('voiceArcInput').value, false);
  });
  document.getElementById('btnRouteCancel').addEventListener('click', closeVoiceArc);

  // Tri-Dock Directory & Cupid
  document.getElementById('btnOpenDirectory').addEventListener('click', () => openDirectorySheet(false));
  document.getElementById('btnToggleNewPersonForm').addEventListener('click', () => {
    state.showNewPersonForm = !state.showNewPersonForm;
    renderDirectorySheet();
  });
  document.getElementById('dockCupidOrb').addEventListener('click', openCupid);

  // Cupid Full-Screen Controls
  document.getElementById('btnCloseCupid').addEventListener('click', closeCupid);
  document.getElementById('btnToggleMatDrawer').addEventListener('click', openMatDrawer);
  document.getElementById('btnCupidPlusMat').addEventListener('click', openMatDrawer);
  document.getElementById('btnCloseMatDrawer').addEventListener('click', closeMatDrawer);
  document.getElementById('cupidMatScrim').addEventListener('click', closeMatDrawer);
  document.getElementById('cupidChatForm').addEventListener('submit', (e) => {
    e.preventDefault();
    const inp = document.getElementById('cupidChatInput');
    const q = inp.value;
    inp.value = '';
    sendCupidQuestion(q);
  });

  // Capture Sheet
  document.querySelectorAll('[data-cap-mode]').forEach((b) => b.addEventListener('click', () => {
    state.capMode = b.dataset.capMode;
    document.querySelectorAll('[data-cap-mode]').forEach((x) => x.classList.toggle('is-active', x === b));
    updateCapturePreview();
  }));
  document.getElementById('capPhotoFile').addEventListener('change', (e) => {
    const f = e.target.files && e.target.files[0];
    document.getElementById('capPhotoName').textContent = f ? `✓ 已选相片：${f.name}` : '选择一张生活或人像照片（自动剥离 EXIF 并提取色域）';
  });
  document.getElementById('capTextarea').addEventListener('input', updateCapturePreview);
  document.querySelectorAll('[data-cap-sample]').forEach((b) => b.addEventListener('click', () => {
    document.getElementById('capTextarea').value = b.dataset.capSample;
    updateCapturePreview();
  }));
  document.getElementById('btnCapAutoRoute').addEventListener('click', () => commitSmartCapture(document.getElementById('capTextarea').value, true));
  document.getElementById('btnCapSaveDirect').addEventListener('click', () => commitSmartCapture(document.getElementById('capTextarea').value, false));

  // Sheets close (debounced against iOS touch ghost-clicks right after openSheet)
  document.getElementById('sheetBackdrop').addEventListener('click', () => {
    if (Date.now() - state.sheetOpenedAt < 260) return;
    closeAllSheets();
  });
  document.querySelectorAll('[data-close-sheet]').forEach((b) => b.addEventListener('click', closeAllSheets));

  // Spotlight
  document.getElementById('spotInput').addEventListener('input', renderSpotlight);
  document.getElementById('btnSpotClear').addEventListener('click', () => { document.getElementById('spotInput').value = ''; renderSpotlight(); });
  document.getElementById('btnSpotClose').addEventListener('click', closeSpotlight);
  document.querySelectorAll('[data-sfilter]').forEach((b) => b.addEventListener('click', () => {
    state.spotFilter = b.dataset.sfilter;
    document.querySelectorAll('[data-sfilter]').forEach((x) => x.classList.toggle('is-active', x === b));
    renderSpotlight();
  }));

  // Lightbox
  document.getElementById('btnLbClose').addEventListener('click', () => document.getElementById('lightboxOverlay').classList.remove('is-open'));
  document.getElementById('btnLbPrev').addEventListener('click', () => { state.lbIndex = (state.lbIndex - 1 + state.lbPhotos.length) % state.lbPhotos.length; renderLightbox(); });
  document.getElementById('btnLbNext').addEventListener('click', () => { state.lbIndex = (state.lbIndex + 1) % state.lbPhotos.length; renderLightbox(); });
  document.getElementById('btnLbFav').addEventListener('click', () => {
    const r = state.lbPhotos[state.lbIndex];
    if (r) toggleMaterialFavorite(currentPerson(), r.id);
  });
  document.getElementById('btnLbSetBg').addEventListener('click', () => {
    const r = state.lbPhotos[state.lbIndex];
    if (r) promoteMaterialToBackground(currentPerson(), r.id);
  });

  // Keyboard shortcuts
  window.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); openSpotlight(); }
    if (e.key === 'Escape') {
      closeSpotlight();
      closeAllSheets();
      closeVoiceArc();
      document.getElementById('lightboxOverlay').classList.remove('is-open');
    }
  });
  window.addEventListener('resize', () => syncChromaForPerson(currentPerson()));
}

async function boot() {
  wireEvents();
  animateOrbs();

  if (state.session && state.session.accessToken) {
    try {
      await refreshBootstrap({ keepPersonId: false });
      closeAuthGate();
      return;
    } catch (_) {
      saveSession(null);
    }
  }
  openAuthGate();
}

boot();
