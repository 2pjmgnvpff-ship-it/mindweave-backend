// journal-items.js — 6 V2 Scale-1 porcelain card archetypes (ZERO top-left eyebrows; pure
// Gregorian date strictly at bottom-right!), BC5 WebKit #203302 Blob audio LRU + stopActiveAudio(),
// QLPreviewController Live Text photo tap (blocked on Silver), & Scale-2/3 B3 cards.
import { h, svg, $ } from '../dom.js';
import { formatDateCn, formatDateFullCn, formatClock, highlightParts, highlightMatches, clip } from '../format.js';
import { BOOKMARK_HAIRLINE_SVG } from '../icons.js';
import { client } from '../api/endpoints.js';
import { store } from '../store.js';
import { whisper } from './toast.js';
import { actions } from '../actions.js';
import { haptic, setAudioSession, mwNativeUI, prefersReducedMotion } from '../native-bridge.js';
import { voiceFsm } from '../voice-fsm.js';
import { getSolarTermForMonth } from '../lib/solar-terms.js';

const MAX_AUDIO_BLOBS = 3;
const STALL_WATCHDOG_MS = 4000;
const audioBlobLru = new Map();
let activeAudio = null;

function cacheAudioUrl(mediaUrl, objectUrl) {
  if (audioBlobLru.has(mediaUrl)) {
    const old = audioBlobLru.get(mediaUrl);
    audioBlobLru.delete(mediaUrl);
    if (old !== objectUrl) URL.revokeObjectURL(old);
  }
  audioBlobLru.set(mediaUrl, objectUrl);
  while (audioBlobLru.size > MAX_AUDIO_BLOBS) {
    const oldestKey = audioBlobLru.keys().next().value;
    const oldestUrl = audioBlobLru.get(oldestKey);
    audioBlobLru.delete(oldestKey);
    if (oldestUrl) URL.revokeObjectURL(oldestUrl);
  }
}

export function stopActiveAudio({ revokeAll = false } = {}) {
  if (activeAudio) {
    const cur = activeAudio;
    activeAudio = null;
    clearTimeout(cur.stallTimer);
    cur.btn?.classList.remove('is-playing', 'is-loading');
    cur.btn?.setAttribute('aria-pressed', 'false');
    try { cur.audio.pause(); cur.audio.src = ''; cur.audio.load(); } catch { /* ignore */ }
    setAudioSession?.('deactivate');
    voiceFsm.releasePlayback();
  }
  if (revokeAll) {
    for (const url of audioBlobLru.values()) { try { URL.revokeObjectURL(url); } catch { /* ignore */ } }
    audioBlobLru.clear();
  }
}

if (typeof window !== 'undefined') window.addEventListener('pagehide', () => stopActiveAudio({ revokeAll: true }));

async function resolveAudioBlobUrl(mediaUrl) {
  if (audioBlobLru.has(mediaUrl)) {
    const hit = audioBlobLru.get(mediaUrl);
    audioBlobLru.delete(mediaUrl);
    audioBlobLru.set(mediaUrl, hit);
    return hit;
  }
  // BC5 WebKit #203302: load via authenticated fetch -> Blob -> createObjectURL (never direct mindweave:// Audio!).
  const res = await client.request('GET', mediaUrl, { raw: true });
  const objUrl = URL.createObjectURL(await res.blob());
  cacheAudioUrl(mediaUrl, objUrl);
  return objUrl;
}

function buildWaveformSvg(seedStr) {
  const ns = 'http://www.w3.org/2000/svg';
  const el = document.createElementNS(ns, 'svg');
  el.setAttribute('class', 'mat-audio-wave');
  el.setAttribute('viewBox', '0 0 112 22');
  el.setAttribute('aria-hidden', 'true');
  let seed = 2166136261;
  for (let i = 0; i < seedStr.length; i++) seed = Math.imul(seed ^ seedStr.charCodeAt(i), 16777619) >>> 0;
  for (let i = 0; i < 28; i++) {
    seed = (seed + 0x6d2b79f5) >>> 0;
    const hPx = Math.round(4 + Math.sin((i / 27) * Math.PI) * (6 + (((seed ^ (seed >>> 15)) >>> 0) % 100) * 0.1));
    const r = document.createElementNS(ns, 'rect');
    r.setAttribute('x', String(i * 4));
    r.setAttribute('y', String(Math.round((22 - hPx) / 2)));
    r.setAttribute('width', '2');
    r.setAttribute('height', String(hPx));
    r.setAttribute('rx', '1');
    el.appendChild(r);
  }
  return el;
}

/** Reusable BC5-safe 28-tick Voice Memo Pill (journal-items, peek-date, archive-history-sheet). */
export function renderAudioPill(m) {
  const mediaUrl = m?.mediaUrl || m?.audioUrl || '';
  const playable = Boolean(mediaUrl);
  const dur = m?.dur || (m?.audioDurationMs ? `${Math.max(1, Math.round(m.audioDurationMs / 1000))}″` : '');
  const btn = h(
    'button',
    { type: 'button', class: 'mat-audio-pill', disabled: !playable, 'aria-pressed': 'false', 'aria-label': playable ? `播放语音 ${dur}` : '语音记录' },
    h('span', { class: 'mat-audio-play', 'aria-hidden': 'true', text: '▶' }),
    buildWaveformSvg(String(m?.id || mediaUrl || 'voice')),
    dur ? h('span', { class: 'mat-audio-dur', text: dur }) : null,
  );
  if (playable) {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      haptic('light');
      if (activeAudio && activeAudio.btn === btn) return stopActiveAudio();
      stopActiveAudio();
      const sessionId = voiceFsm.acquire('playback');
      if (!sessionId) return whisper('当前正在录音，请稍后再听', 2000);
      btn.classList.add('is-loading');
      try {
        const blobUrl = await resolveAudioBlobUrl(mediaUrl);
        if (voiceFsm.getState().sessionId !== sessionId) return;
        const audio = new Audio(blobUrl);
        const entry = { audio, btn, stallTimer: 0, sessionId, mediaUrl };
        activeAudio = entry;
        setAudioSession?.('activate');
        const done = () => { if (activeAudio === entry) stopActiveAudio(); };
        entry.stallTimer = setTimeout(() => {
          if (activeAudio === entry && !btn.classList.contains('is-playing')) { stopActiveAudio(); whisper('这段语音加载稍慢，请再试一次', 2400); }
        }, STALL_WATCHDOG_MS);
        audio.addEventListener('playing', () => { clearTimeout(entry.stallTimer); btn.classList.remove('is-loading'); btn.classList.add('is-playing'); btn.setAttribute('aria-pressed', 'true'); }, { once: true });
        audio.addEventListener('ended', done, { once: true });
        audio.addEventListener('error', () => { done(); whisper('这段语音暂时放不出来', 2400); }, { once: true });
        await audio.play();
      } catch {
        stopActiveAudio();
        whisper('这段语音暂时放不出来', 2400);
      }
    });
  }
  return btn;
}

function renderBody(text, query, matchedTerms) {
  const p = h('p', { class: 'mat-body' });
  const segs = typeof highlightMatches === 'function' ? highlightMatches(text, query, matchedTerms) : highlightParts(text, query);
  for (const seg of segs || [{ text: String(text || ''), hit: false }]) {
    p.appendChild(seg.hit ? h('mark', { class: 'spot-hit', text: seg.text }) : document.createTextNode(seg.text));
  }
  return p;
}

function formatSlipFooterDate(m, tier) {
  const raw = m.happenedOn || m.createdAt;
  const ym = /^(\d{4})-/.exec(String(raw || ''));
  const base = ym && Number(ym[1]) !== new Date().getFullYear() ? formatDateFullCn(raw) : formatDateCn(raw);
  if (tier === 'silver') {
    const clk = formatClock(m.createdAt);
    return clk && base ? `${base} ${clk}` : base || clk;
  }
  return base;
}

function renderFooter(m, tier, isPencil, personId) {
  const dateText = formatSlipFooterDate(m, tier);
  const left = [];
  if (m.pinned) {
    left.push(h('span', { class: 'mat-pin-mark', 'aria-label': '已置顶' }, BOOKMARK_HAIRLINE_SVG ? svg(BOOKMARK_HAIRLINE_SVG, 'mat-pin-icon') : h('span', { class: 'mw-dot is-gold' })));
  }
  if (isPencil) {
    left.push(h('button', { type: 'button', class: 'mat-pencil-confirm', text: '待确认 · 轻点核对', onClick: (e) => { e.stopPropagation(); actions.openPeek?.(m.id, store.get().people[personId]); } }));
  }
  return h('footer', { class: 'mat-foot' }, h('span', { class: 'mat-foot-left' }, ...left), dateText ? h('time', { class: 'mat-sign', dateTime: String(m.happenedOn || m.createdAt || ''), text: dateText }) : null);
}

function attachLongPress(card, m, ctx) {
  if (!ctx.onContextMenu) return;
  let timer = 0, sx = 0, sy = 0, fired = false;
  const clearT = () => { if (timer) { clearTimeout(timer); timer = 0; } };
  card.addEventListener('touchstart', (e) => {
    if (e.touches.length !== 1) return clearT();
    fired = false; sx = e.touches[0].clientX; sy = e.touches[0].clientY;
    timer = setTimeout(() => { timer = 0; fired = true; card.dataset.suppressClick = '1'; setTimeout(() => { delete card.dataset.suppressClick; }, 320); ctx.onContextMenu(m, card); }, 480);
  }, { passive: true });
  card.addEventListener('touchmove', (e) => { if (timer && e.touches.length && Math.hypot(e.touches[0].clientX - sx, e.touches[0].clientY - sy) > 12) clearT(); }, { passive: true });
  card.addEventListener('touchend', clearT, { passive: true });
  card.addEventListener('touchcancel', clearT, { passive: true });
  card.addEventListener('contextmenu', (e) => { e.preventDefault(); if (!fired) ctx.onContextMenu(m, card); });
}

/** Scale-1 Porcelain Slip (6 archetypes, level 0deg, ZERO top-left eyebrow; bottom-right date!). */
export function renderMaterial(m, person, ctx = {}) {
  const tier = person?.tier || 'crystal';
  const isSilver = tier === 'silver';
  const isPencil = m.presentation === 'pencil';
  const showPhoto = !isSilver && m.type === 'photo' && Boolean(m.mediaUrl);
  const isVoice = m.type === 'voice' || Boolean(m.audioUrl);
  const body = String(m.body || m.snippet || '').trim();
  const archetype = isSilver ? 'is-silver-evidence' : showPhoto ? 'is-photo' : isVoice ? 'is-voice' : Array.from(body).length <= 34 ? 'is-short' : 'is-narrative';
  const veiled = isSilver && Boolean(ctx.silverVeiled);

  const card = h('article', {
    class: ['mat-card', archetype, m.pinned && 'is-pinned', isPencil && 'is-pencil', veiled && 'is-veiled'],
    dataset: { materialId: m.id, tier },
    role: 'button',
    tabindex: '0',
    'aria-label': veiled ? '银盾边界记录，轻点查看原文' : `${clip(body, 40)}，${formatSlipFooterDate(m, tier)}`,
  });

  if (veiled) {
    card.append(
      h('div', { class: 'mat-veil-cover' }, h('p', { class: 'mat-veil-title', text: '边界留痕已收起' }), h('button', { type: 'button', class: 'mat-veil-btn', text: '查看原文', onClick: (e) => { e.stopPropagation(); haptic('rigid'); ctx.onToggleVeil?.(false); } })),
      renderFooter(m, tier, isPencil, person?.id),
    );
    return card;
  }

  if (showPhoto) {
    const title = `${formatDateCn(m.happenedOn || m.createdAt)} · ${person?.nameCn || ''}`.trim();
    const mat = h('div', { class: 'mat-photo-mat', onClick: (e) => { e.stopPropagation(); haptic('light'); mwNativeUI?.previewMedia?.({ url: m.mediaUrl, title, tier }); } }, h('img', { class: 'mat-photo-img', src: m.mediaUrl, alt: clip(body, 24) || '手帐照片', loading: 'lazy', decoding: 'async' }));
    const cap = body ? renderBody(body, ctx.query, m.matchedTerms) : null;
    if (cap) cap.classList.add('mat-photo-body');
    card.append(mat, cap, renderFooter(m, tier, isPencil, person?.id));
  } else if (isVoice) {
    card.append(body ? renderBody(body, ctx.query, m.matchedTerms) : null, h('div', { class: 'mat-voice-row' }, renderAudioPill(m)), renderFooter(m, tier, isPencil, person?.id));
  } else {
    card.append(renderBody(body || '一页空白手帐', ctx.query, m.matchedTerms), renderFooter(m, tier, isPencil, person?.id));
  }

  card.addEventListener('click', (e) => { if (!card.dataset.suppressClick && !e.target.closest('button, input, textarea, a')) actions.openPeek?.(m.id, person); });
  card.addEventListener('keydown', (e) => { if (e.target === card && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); actions.openPeek?.(m.id, person); } });
  attachLongPress(card, m, ctx);
  return card;
}

export function renderPendingSlip(p) {
  const failed = p.state === 'failed', queued = p.state === 'queued';
  return h(
    'article',
    { class: ['mat-card', 'is-short', 'is-drying', failed && 'is-failed', queued && 'is-queued'], dataset: { pendingId: p.id }, 'aria-busy': String(!failed && !queued) },
    h('p', { class: 'mat-body', text: p.text || '一段语音记录' }),
    h('footer', { class: 'mat-foot' }, h('span', { class: 'drying-label', text: failed ? '未能发出' : queued ? '离线暂存 · 联网后自动补传' : '正在整理入册…' })),
  );
}

/** Zero-fake progressive density graphic for Scale-2 Monthly Cover (B3 + BC6 weeklyCounts). */
function renderDensityMark(bucket, tier) {
  const ns = 'http://www.w3.org/2000/svg';
  if (tier === 'silver') {
    const weeks = Array.isArray(bucket.weeklyCounts) && bucket.weeklyCounts.length === 4 ? bucket.weeklyCounts : null;
    const svgEl = document.createElementNS(ns, 'svg');
    svgEl.setAttribute('class', 'scale2-cadence-svg is-silver');
    svgEl.setAttribute('viewBox', '0 0 88 10');
    svgEl.setAttribute('aria-hidden', 'true');
    const maxW = weeks ? Math.max(1, ...weeks) : Math.max(1, bucket.count || 1);
    for (let i = 0; i < 4; i++) {
      const val = weeks ? weeks[i] : (i === 0 ? bucket.count || 0 : 0);
      const r = document.createElementNS(ns, 'rect');
      r.setAttribute('x', String(i * 22)); r.setAttribute('y', '3'); r.setAttribute('width', '18'); r.setAttribute('height', '4'); r.setAttribute('rx', '2');
      r.setAttribute('opacity', String(val > 0 ? Math.min(0.85, 0.25 + (val / maxW) * 0.6) : 0.14));
      svgEl.appendChild(r);
    }
    return svgEl;
  }
  const level = Math.min(5, Math.max(1, Math.round(Math.log(1 + (bucket.count || 0)) * 1.6)));
  const strip = h('span', { class: 'scale2-cadence-dots', 'aria-hidden': 'true' });
  for (let i = 0; i < 5; i++) strip.appendChild(h('i', { class: ['cadence-dot', i < level && 'is-on'] }));
  return strip;
}

export function renderScale2Card(bucket, person, { onSelect } = {}) {
  const tier = person?.tier || 'crystal';
  const showCover = tier !== 'silver' && Boolean(bucket.coverPhotoUrl);
  const solarTerm = getSolarTermForMonth ? getSolarTermForMonth(bucket.year, bucket.month) : '';
  const ym = bucket.bucket || `${bucket.year}-${String(bucket.month).padStart(2, '0')}`;
  return h(
    'button',
    { type: 'button', class: ['scale2-card', `tier-${tier}`, showCover && 'has-cover'], dataset: { month: ym }, onClick: () => { haptic('porcelainTick'); onSelect?.(bucket); } },
    h('div', { class: 'scale2-card-head' }, h('strong', { class: 'scale2-month', text: `${bucket.month}月` }), solarTerm ? h('span', { class: 'scale2-solar-term', text: solarTerm }) : null),
    showCover ? h('div', { class: 'scale2-cover-mat' }, h('img', { src: bucket.coverPhotoUrl, alt: '', loading: 'lazy', decoding: 'async' })) : h('p', { class: 'scale2-quote', text: clip(bucket.highlightQuote || `${bucket.count || 0} 条记录`, 36) }),
    h('div', { class: 'scale2-card-foot' }, renderDensityMark(bucket, tier), h('span', { class: 'scale2-count', text: `${bucket.count || 0} 条` })),
  );
}

export function renderScale3Card(yearGroup, person, { onSelect } = {}) {
  const tier = person?.tier || 'crystal';
  return h(
    'button',
    { type: 'button', class: ['scale3-card', `tier-${tier}`], dataset: { year: String(yearGroup.year) }, onClick: () => { haptic('yearDetent'); onSelect?.(yearGroup); } },
    h('strong', { class: 'scale3-year', text: `${yearGroup.year}年` }),
    h('span', { class: 'scale3-meta', text: `${yearGroup.months.length} 个月 · ${yearGroup.totalCount} 条记录` }),
    h('span', { class: 'scale3-chevron', 'aria-hidden': 'true', text: '›' }),
  );
}

export function flashMaterial(materialId) {
  const el = document.querySelector(`[data-material-id="${CSS.escape(String(materialId))}"]`);
  if (!el) return false;
  const scroll = $('#journalScroll');
  if (scroll && scroll.contains(el)) {
    const r = el.getBoundingClientRect(), sr = scroll.getBoundingClientRect();
    scroll.scrollTo({ top: Math.max(0, r.top - sr.top + scroll.scrollTop - Math.max(88, (sr.height - r.height) / 2)), behavior: prefersReducedMotion() ? 'auto' : 'smooth' });
  }
  el.classList.remove('is-flash');
  void el.offsetWidth;
  el.classList.add('is-flash');
  setTimeout(() => el.classList.remove('is-flash'), 1400);
  return true;
}

export function peopleCount() {
  return Object.keys(store.get().people || {}).length;
}
