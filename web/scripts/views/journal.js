// journal.js — 3-Scale Porcelain Journal (纸页 / 月册 / 年轮), Fenwick virtual-river, Ghost Spine,
// dynamic 2-finger pinch, BC6 /search + matchedTerms, B2 Gold-only 往年今日, native UIMenu long-press.
import { h, clear, svg, $ } from '../dom.js';
import { api, client } from '../api/endpoints.js';
import { store, setMaterials, invalidateMaterials, patchMaterial, removeMaterial, upsertMaterial } from '../store.js';
import { formatDateFullCn, todayIso, clip } from '../format.js';
import { GEAR_SVG, SEARCH_SVG, CLOSE_SVG, CUPID_IMG } from '../icons.js';
import { renderMaterial, renderPendingSlip, renderScale2Card, renderScale3Card, flashMaterial, stopActiveAudio } from './journal-items.js';
import { openPeekOverlay, closePeek } from './peek-date.js';
import { renderUpcomingRibbon, openVellumPocket } from './vellum-pocket.js';
import { renderArrow, strikeArrow } from './arrow.js';
import { showUndoToast, whisper } from './toast.js';
import { actions } from '../actions.js';
import { haptic, copyText, mwNativeUI, sharePayload, prefersReducedMotion } from '../native-bridge.js';
import { createVirtualRiver } from '../lib/virtual-river.js';
import { attachJournalPinch } from '../lib/journal-pinch.js';
import { createOrb } from '../lib/orb.js';
import { flipScaleMorph } from '../lib/spring-waapi.js';
import { createGhostScrubber } from '../journal-scrubber.js';

const EMPTY_STARTERS = {
  gold: ['记一件最近暖到心里的细节', '记下生日或想一起去的地方', '随口说一句近况'],
  silver: ['客观记下今天的时间与原话', '记下一次需要留意的边界事件'],
  crystal: ['记下偏好的口味或习惯', '记下最近一次重要约定'],
};

let scale = 1;
let scaleStack = [];
let activeMonthYm = '';
let pendingFlash = null;
let lastRenderKey = '';
let searchDebounce = 0;
let searchAc = null;
let searchHits = null;
let activeFacet = 'all';

const remindersCache = new Map();
const bucketsCache = new Map();
const onThisDayDismissed = new Set();
const silverVeilByPerson = new Map();
let silverSessionWhispered = false;
let river = null;
let scrubber = null;
let pinchAc = null;
let heroObserver = null;

export function resetJournalCaches() {
  stopActiveAudio({ revokeAll: true });
  remindersCache.clear();
  bucketsCache.clear();
  onThisDayDismissed.clear();
  silverVeilByPerson.clear();
  silverSessionWhispered = false;
  searchHits = null;
  scale = 1;
  scaleStack = [];
  lastRenderKey = '';
}

const isSilverVeiled = (pid) => silverVeilByPerson.get(pid) === true;

async function fetchBuckets(personId, { force = false } = {}) {
  if (!personId) return null;
  if (!force && bucketsCache.has(personId)) return bucketsCache.get(personId);
  try {
    const res = typeof api.timelineBuckets === 'function'
      ? await api.timelineBuckets(personId)
      : await client.get(`/persons/${encodeURIComponent(personId)}/timeline-buckets`);
    const data = { totalCount: res?.totalCount || 0, years: res?.years || [], buckets: res?.buckets || [] };
    bucketsCache.set(personId, data);
    return data;
  } catch { return null; }
}

async function fetchReminders(personId, { force = false } = {}) {
  if (!personId) return [];
  if (!force && remindersCache.has(personId)) return remindersCache.get(personId);
  try {
    const list = (await api.reminders(personId))?.items || [];
    remindersCache.set(personId, list);
    return list;
  } catch { return []; }
}

export async function loadMaterials(personId, { append = false } = {}) {
  if (!personId) return;
  const s = store.get();
  const prev = s.materials[personId];
  if (prev?.loading) return;
  store.set({ materials: { ...s.materials, [personId]: { items: prev?.items || [], hasMore: false, nextCursor: null, ...prev, loading: true } } });
  try {
    setMaterials(personId, await api.materials(personId, { cursor: append ? prev?.nextCursor : undefined, limit: 40 }), { append });
    fetchReminders(personId).then(() => renderJournal(store.get()));
    fetchBuckets(personId).then(() => syncScrubber(personId));
  } catch (err) {
    const cur = store.get().materials;
    store.set({ materials: { ...cur, [personId]: { ...(cur[personId] || {}), items: cur[personId]?.items || [], loading: false, loaded: true, failed: true } } });
    whisper(err?.message || '手帐页载入稍慢，请重试', 3000);
  }
}

export function reloadMaterials(personId) {
  invalidateMaterials(personId);
  bucketsCache.delete(personId);
  remindersCache.delete(personId);
  if (store.get().activePersonId === personId) loadMaterials(personId);
}

export function openPerson(personId, { materialId = null } = {}) {
  const person = store.get().people[personId];
  if (!person) return;
  closePeek({ silentHaptic: true });
  stopActiveAudio();
  scale = 1; scaleStack = []; activeMonthYm = ''; searchHits = null; activeFacet = 'all'; pendingFlash = materialId;
  const inp = $('#spotlightInput');
  if (inp) inp.value = '';
  // Reset scrollTop BEFORE store.set synchronously mounts virtual-river so Silver/short journals NEVER mount clipped above viewport!
  if (!materialId && $('#journalScroll')) $('#journalScroll').scrollTop = 0;
  $('#topbar')?.classList.remove('is-collapsed');
  $('#folioBar')?.classList.remove('is-visible');
  store.set({ view: 'journal', activePersonId: personId, journalFilter: 'all', searchOpen: false, searchQuery: '' });
  try { localStorage.setItem('mw.lastPerson', personId); } catch { /* ignore */ }
  if (person.tier === 'silver' && !silverSessionWhispered) { silverSessionWhispered = true; whisper('内容仅自己可见', 2400); }
  const m = store.get().materials[personId];
  if (!m || (!m.loaded && !m.loading)) loadMaterials(personId);
  else if (materialId) { pendingFlash = null; requestAnimationFrame(() => flashMaterial(materialId)); }
  strikeArrow();
}

export function scrollJournalToTop(smooth = true) {
  const scroller = $('#journalScroll');
  if (!scroller) return false;
  if (scroller.scrollTop > 120) haptic('porcelainTick');
  const behavior = smooth && !prefersReducedMotion() ? 'smooth' : 'auto';
  if (river?.scrollToTop) river.scrollToTop({ behavior });
  else scroller.scrollTo({ top: 0, behavior });
  return true;
}

async function handleSlipContextMenu(m, cardEl, person) {
  const tier = person?.tier || 'crystal';
  const isSilver = tier === 'silver';
  const r = cardEl.getBoundingClientRect();
  const items = [
    { id: 'pin', label: m.pinned ? '取消置顶' : '置顶', title: m.pinned ? '取消置顶' : '置顶', symbol: 'pin', group: 1 },
    { id: 'edit', label: '编辑内容', title: '编辑内容', symbol: 'pencil', group: 1 },
    { id: 'reassign', label: '换给其他人', title: '换给其他人', symbol: 'person.crop.circle.badge.arrow.right', group: 1 },
    { id: 'copy', label: '复制文字', title: '复制文字', symbol: 'doc.on.doc', group: 1 },
    { id: 'chat', label: '和丘比特聊这条', title: '和丘比特聊这条', symbol: 'bubble.left', group: 2 },
    ...(!isSilver ? [{ id: 'share', label: '分享手帐卡片', title: '分享手帐卡片', symbol: 'square.and.arrow.up', group: 2 }] : []),
    { id: 'delete', label: '删除', title: '删除', symbol: 'trash', role: 'destructive', destructive: true, group: 3 },
  ];
  const res = await mwNativeUI?.showContextMenu?.({ rect: { x: r.left, y: r.top, width: r.width, height: r.height }, cornerRadius: 24, previewText: clip(m.body || m.snippet || '', 80), items, tier });
  const id = res?.selectedId;
  if (!id) return;
  if (id === 'pin') {
    const next = !m.pinned;
    patchMaterial(person.id, m.id, (await api.patchMaterial(m.id, { pinned: next }, m.version).catch(() => null)) || { pinned: next });
    whisper(next ? '已置顶' : '已取消置顶', 1800);
  } else if (id === 'edit') openPeekOverlay(m.id, person, { startEditing: true, onMutated: () => reloadMaterials(person.id) });
  else if (id === 'reassign') {
    actions.openPeopleSheet?.({
      title: '换给哪一位人物？',
      onSelect: async (t) => {
        if (!t || t.id === person.id) return;
        await api.patchMaterial(m.id, { personId: t.id }, m.version);
        removeMaterial(person.id, m.id); reloadMaterials(t.id); whisper(`已移至「${t.nameCn}」`, 2200);
      },
    });
  } else if (id === 'copy') {
    if (isSilver && mwNativeUI?.copySensitive) await mwNativeUI.copySensitive(m.body || ''); else await copyText(m.body || '');
    whisper(isSilver ? '已仅限本机复制（5 分钟自动清除）' : '已复制文字', 2000);
  } else if (id === 'chat') actions.openChat?.(person.id, { materialId: m.id });
  else if (id === 'share' && !isSilver) await sharePayload?.({ title: `${person.nameCn} · 手帐`, text: m.body || '' });
  else if (id === 'delete') {
    const backup = { ...m }; removeMaterial(person.id, m.id); let undone = false;
    showUndoToast('已删除这条手帐', () => { undone = true; upsertMaterial(person.id, backup); }, { onExpire: () => { if (!undone) api.deleteMaterial(m.id).catch(() => {}); } });
  }
}

function renderOnThisDayCard(person, items) {
  if (!person || person.tier !== 'gold' || onThisDayDismissed.has(person.id)) return null;
  const today = todayIso(), curYear = Number(today.slice(0, 4)), curMmDd = today.slice(5, 10);
  const match = (items || []).find((m) => { const d = String(m.happenedOn || m.createdAt || ''); return d.length >= 10 && Number(d.slice(0, 4)) < curYear && d.slice(5, 10) === curMmDd; });
  if (!match) return null;
  const diff = curYear - Number(String(match.happenedOn || match.createdAt).slice(0, 4));
  return h('section', { class: 'mw-otd-card on-this-day-card', 'aria-label': '往年今日' },
    h('button', { type: 'button', class: 'mw-otd-close on-this-day-close', 'aria-label': '收起往年今日', text: '×', onClick: (e) => { e.stopPropagation(); onThisDayDismissed.add(person.id); renderJournal(store.get()); } }),
    h('p', { class: 'mw-otd-body on-this-day-body', text: clip(match.body || match.snippet || '', 90) }),
    h('footer', { class: 'mw-otd-foot mat-foot' },
      h('span', { class: 'mw-dot is-gold', 'aria-hidden': 'true' }),
      h('time', { class: 'mat-sign', text: `${diff === 1 ? '去年今日' : `${diff} 年前的今天`} · ${formatDateFullCn(match.happenedOn || match.createdAt)}` }),
    ),
  );
}

async function runSemanticSearch(personId, q) {
  searchAc?.abort();
  const trimmed = String(q || '').trim();
  if (!trimmed && activeFacet === 'all') { searchHits = null; renderJournal(store.get()); return; }
  searchAc = new AbortController();
  const query = { q: trimmed || undefined, limit: 40, ...(activeFacet === 'pinned' ? { pinned: true } : activeFacet !== 'all' ? { kind: activeFacet } : {}) };
  try {
    const res = typeof api.personSearch === 'function' ? await api.personSearch(personId, query, { signal: searchAc.signal }) : await client.get(`/persons/${encodeURIComponent(personId)}/search`, { query, signal: searchAc.signal });
    // BC6: unpack nested hit.material alongside matchKind & matchedTerms!
    const unpacked = (res?.items || []).map((hit) => ({ ...(hit?.material || hit), matchKind: hit?.matchKind, matchedTerms: hit?.matchedTerms || [] }));
    searchHits = { query: trimmed, items: unpacked, expandedTerms: res?.expandedTerms || [] };
    renderJournal(store.get());
  } catch (err) { if (err?.name !== 'AbortError') whisper(err?.message || '搜索未能完成', 2400); }
}

const CN_MONTHS = ['', '一月', '二月', '三月', '四月', '五月', '六月', '七月', '八月', '九月', '十月', '十一月', '十二月'];
let selectedScale2Year = null;

function editorialMonthLabel(items) {
  if (scale === 2) return selectedScale2Year ? `${selectedScale2Year}年` : '月册';
  if (scale === 3) return '年轮';
  if (activeMonthYm && /^\d{4}-\d{2}/.test(activeMonthYm)) {
    const m = Number(activeMonthYm.slice(5, 7));
    if (m >= 1 && m <= 12) return CN_MONTHS[m];
  }
  const latest = (items || [])[0];
  const raw = String(latest?.happenedOn || latest?.createdAt || '');
  const mm = /^\d{4}-(\d{2})/.exec(raw);
  const idx = mm ? Number(mm[1]) : new Date().getMonth() + 1;
  return CN_MONTHS[idx] || '';
}

const openSearch = () => {
  haptic('light');
  const scroller = $('#journalScroll');
  if (scroller) scroller.scrollTop = 0;
  store.set({ searchOpen: true });
  requestAnimationFrame(() => $('#spotlightInput')?.focus({ preventScroll: true }));
};
const closeSearch = () => { searchAc?.abort(); searchHits = null; activeFacet = 'all'; const i = $('#spotlightInput'); if (i) i.value = ''; store.set({ searchOpen: false, searchQuery: '' }); };

function setScale(nextScale, { anchorEl = null, targetYm = '', year = null } = {}) {
  const clamped = Math.max(1, Math.min(3, nextScale));
  if (year != null) selectedScale2Year = year;
  if (clamped === scale && !targetYm && year == null) return;
  const scroller = $('#journalScroll');
  if (clamped > scale && scroller) scaleStack.push({ scale, scrollTop: scroller.scrollTop, ym: activeMonthYm });
  scale = clamped;
  if (targetYm) activeMonthYm = targetYm;
  if (scroller) scroller.scrollTop = 0;
  $('#journalCanvas')?.classList.remove('is-scrolled');
  haptic(clamped === 3 ? 'yearDetent' : 'porcelainTick');
  if (anchorEl && flipScaleMorph) flipScaleMorph({ fromEl: anchorEl, toEl: scroller, onDone: () => {} });
  lastRenderKey = '';
  renderJournal(store.get());
  if (clamped === 1 && targetYm) {
    const s = store.get();
    const items = s.materials[s.activePersonId]?.items || [];
    const idx = items.findIndex((m) => String(m.happenedOn || m.createdAt || '').startsWith(targetYm));
    if (idx > 0 && river?.scrollToIndex) requestAnimationFrame(() => river?.scrollToIndex(idx, { behavior: 'auto' }));
  }
}

function syncScrubber(personId) {
  if (!scrubber) return;
  const data = bucketsCache.get(personId), mats = store.get().materials[personId]?.items || [];
  const months = (data?.buckets || []).map((b) => ({ ym: b.bucket || `${b.year}-${String(b.month).padStart(2, '0')}`, year: b.year, month: b.month, label: `${b.year} · ${b.month}月`, count: b.count || 0 }));
  scrubber.update({ months, totalSlips: Math.max(data?.totalCount || 0, mats.length), activeYm: activeMonthYm });
}

function renderEmptyState(person) {
  const tier = person.tier || 'crystal';
  const emblem = tier === 'gold' ? h('div', { class: 'empty-cupid-medallion' }, h('img', { src: CUPID_IMG, alt: '', draggable: 'false' })) : createOrb({ size: 'starter', state: 'idle' });
  return h('div', { class: ['journal-empty-state', `tier-${tier}`] }, emblem, h('p', { class: 'empty-state-title', text: `关于「${person.nameCn}」的这一页还空着` }),
    h('div', { class: 'empty-starter-row' }, ...(EMPTY_STARTERS[tier] || EMPTY_STARTERS.gold).map((prompt) => h('button', { type: 'button', class: 'empty-starter-chip', text: prompt, onClick: () => { haptic('light'); actions.openTextCapture?.({ personId: person.id, prompt }); } }))));
}

function buildHero(person, reminders, items) {
  const tier = person.tier || 'crystal', veiled = isSilverVeiled(person.id);
  const refreshRem = () => fetchReminders(person.id, { force: true }).then(() => renderJournal(store.get()));
  const monthText = editorialMonthLabel(items);
  const metaItems = [
    person.roleTag ? h('span', { class: 'journal-tier-tag journal-hero-tag', text: person.roleTag }) : null,
    tier === 'silver'
      ? h('button', { type: 'button', class: ['silver-veil-toggle', veiled && 'is-veiled'], text: veiled ? '显示正文' : '隐藏内容', onClick: () => { haptic('rigid'); silverVeilByPerson.set(person.id, !veiled); lastRenderKey = ''; renderJournal(store.get()); } })
      : null,
  ].filter(Boolean);

  return h('header', { class: ['journal-hero', `tier-${tier}`] },
    h('div', { class: 'journal-hero-top' },
      h('div', { class: 'journal-hero-title-group' },
        h('h1', { class: 'journal-hero-name', text: person.nameCn }),
        monthText ? h('button', { type: 'button', class: 'journal-hero-month', text: monthText, 'aria-label': '切换手帐时间尺度', onClick: () => setScale(scale === 1 ? 2 : scale === 2 ? 3 : 1) }) : null,
      ),
    ),
    metaItems.length ? h('div', { class: 'journal-hero-meta' }, ...metaItems) : null,
    h('div', { class: 'journal-scale-bar', role: 'tablist', 'aria-label': '手帐时间尺度' },
      ...[[1, '纸页'], [2, '月册'], [3, '年轮']].map(([sc, lbl]) => h('button', { type: 'button', role: 'tab', class: ['scale-pill-btn', 'journal-scale-btn', scale === sc && 'is-active'], 'aria-selected': String(scale === sc), text: lbl, onClick: () => setScale(sc) })),
      h('button', { type: 'button', class: 'vellum-entry-link', text: tier === 'silver' ? '关键节点 ›' : '人物档案 ›', onClick: () => openVellumPocket(person, { onJumpToRecord: (rid) => { scale = 1; closeSearch(); requestAnimationFrame(() => flashMaterial(rid)); }, onMutated: refreshRem }) })),
    scale === 1 ? renderUpcomingRibbon(person, reminders, { onOpenVellum: () => openVellumPocket(person, { onMutated: refreshRem }), onMutated: refreshRem }) : null,
  );
}

function renderScale2or3(scroll, person, hero) {
  clear(scroll); scroll.appendChild(hero);
  const data = bucketsCache.get(person.id);
  if (!data) { scroll.appendChild(h('p', { class: 'page-end-mark', text: '正在整理时间册…' })); fetchBuckets(person.id).then(() => { lastRenderKey = ''; renderJournal(store.get()); }); return; }
  const buckets = data.buckets || [];
  if (!buckets.length) { scroll.appendChild(renderEmptyState(person)); return; }
  if (scale === 2) {
    const years = [...new Set(buckets.map((b) => b.year))].sort((a, b) => b - a);
    if (years.length > 0 && (!selectedScale2Year || !years.includes(selectedScale2Year))) selectedScale2Year = years[0];
    if (years.length > 1) {
      const yearBar = h('div', { class: 'scale2-year-bar', role: 'tablist', 'aria-label': '按年份筛选月册' },
        ...years.map((yr) => h('button', {
          type: 'button',
          role: 'tab',
          class: ['scale2-year-pill', selectedScale2Year === yr && 'is-active'],
          'aria-selected': String(selectedScale2Year === yr),
          text: `${yr}`,
          onClick: () => { haptic('porcelainTick'); selectedScale2Year = yr; lastRenderKey = ''; renderJournal(store.get()); },
        })),
      );
      scroll.appendChild(yearBar);
    }
    const visibleBuckets = selectedScale2Year ? buckets.filter((b) => b.year === selectedScale2Year) : buckets;
    const showYearOnCard = years.length > 1 && !selectedScale2Year;
    const grid = h('div', { class: 'scale2-grid' });
    for (const b of visibleBuckets) grid.appendChild(renderScale2Card(b, person, { showYear: showYearOnCard, onSelect: (bk) => setScale(1, { targetYm: bk.bucket || `${bk.year}-${String(bk.month).padStart(2, '0')}` }) }));
    scroll.appendChild(grid);
    return;
  }
  const byYear = new Map();
  for (const b of buckets) { if (!byYear.has(b.year)) byYear.set(b.year, { year: b.year, months: [], totalCount: 0 }); const g = byYear.get(b.year); g.months.push(b); g.totalCount += b.count || 0; }
  const list = h('div', { class: 'scale3-list' });
  for (const yg of [...byYear.values()].sort((a, b) => b.year - a.year)) list.appendChild(renderScale3Card(yg, person, { onSelect: () => setScale(2, { year: yg.year }) }));
  scroll.appendChild(list);
}

let lastRenderedPersonId = '';
function renderPage(s) {
  const scroll = $('#journalScroll'), person = s.people[s.activePersonId];
  if (!scroll || !person) return;
  if (person.id !== lastRenderedPersonId) {
    lastRenderedPersonId = person.id;
    selectedScale2Year = null;
    if (!pendingFlash) scroll.scrollTop = 0;
  }
  const mats = s.materials[person.id], allItems = mats?.items || [], pending = s.pending.filter((p) => p.pageId === person.id);
  const hero = buildHero(person, remindersCache.get(person.id) || [], allItems);
  if (scale !== 1) { river?.destroy?.(); river = null; renderScale2or3(scroll, person, hero); return; }

  const query = s.searchOpen ? s.searchQuery.trim() : '';
  const visible = s.searchOpen && searchHits ? searchHits.items : allItems;
  const countEl = $('#spotlightCount');
  if (countEl) { countEl.hidden = !s.searchOpen; if (s.searchOpen) countEl.textContent = `${visible.length} 条`; }

  const headerStack = h('div', { class: 'journal-header-stack' }, hero);
  if (s.searchOpen) {
    const facets = [['all', '全部'], ...(person.tier !== 'silver' ? [['photo', '照片']] : []), ['voice', '语音'], ['pinned', '置顶']];
    headerStack.appendChild(h('div', { class: 'search-facet-row' }, ...facets.map(([f, lbl]) => h('button', { type: 'button', class: ['search-facet-chip', activeFacet === f && 'is-active', activeFacet === f && 'is-selected'], text: lbl, onClick: () => { activeFacet = f; runSemanticSearch(person.id, store.get().searchQuery); } }))));
    if (searchHits?.expandedTerms?.length) headerStack.appendChild(h('p', { class: 'search-synonym-whisper', text: `同时检索：${searchHits.expandedTerms.slice(0, 4).join(' · ')}` }));
  } else {
    const otd = renderOnThisDayCard(person, allItems);
    if (otd) headerStack.appendChild(otd);
    for (const p of pending) headerStack.appendChild(renderPendingSlip(p));
  }

  clear(scroll);
  const stage = h('div', { class: 'journal-virtual-stage journal-river-stage', style: { position: 'relative', width: '100%' } });
  scroll.append(headerStack, stage);
  heroObserver?.disconnect();
  if (typeof IntersectionObserver === 'function') {
    heroObserver = new IntersectionObserver((es) => {
      const collapsed = !es[0]?.isIntersecting;
      $('#topbar')?.classList.toggle('is-collapsed', collapsed);
      $('#folioBar')?.classList.toggle('is-visible', collapsed);
    }, { root: scroll, threshold: 0.1 });
    heroObserver.observe(hero);
  }

  river?.destroy?.();
  if (!visible.length) {
    river = null;
    if (s.searchOpen) {
      stage.appendChild(h('div', { class: 'search-empty-wrap' },
        h('p', { class: 'search-empty-whisper', text: query ? `这一页里暂时没找到「${query}」` : '输入关键词或选上方标签检索' }),
        h('button', { type: 'button', class: 'sheet-btn quiet', text: '去其他人物手帐里找', onClick: () => { closeSearch(); actions.openPeopleSheet?.({ query }); } }),
      ));
    } else if (mats?.loaded && !pending.length) stage.appendChild(renderEmptyState(person));
    else if (!mats?.loaded) stage.appendChild(h('p', { class: 'page-end-mark', text: '正在翻开…' }));
    return;
  }

  const ctx = { query, silverVeiled: isSilverVeiled(person.id), onToggleVeil: (v) => { silverVeilByPerson.set(person.id, v); lastRenderKey = ''; renderJournal(store.get()); }, onContextMenu: (m, cardEl) => handleSlipContextMenu(m, cardEl, person) };
  if (typeof createVirtualRiver === 'function' && visible.length > 50) {
    river = createVirtualRiver({ scroller: scroll, stage, headerEl: headerStack, renderItem: (m) => renderMaterial(m, person, ctx), estimateHeight: (m) => (m.type === 'photo' && person.tier !== 'silver' ? 320 : m.type === 'voice' ? 148 : 112) });
    river.setItems(visible);
  } else {
    river = null;
    stage.classList.add('is-inflow');
    const frag = document.createDocumentFragment();
    for (const m of visible) frag.appendChild(renderMaterial(m, person, ctx));
    stage.appendChild(frag);
  }
  if (mats?.hasMore && !s.searchOpen) scroll.appendChild(h('button', { type: 'button', class: 'load-more-btn', text: '载入更早记录', onClick: () => loadMaterials(person.id, { append: true }) }));
  syncScrubber(person.id);
  if (pendingFlash) { const tid = pendingFlash; pendingFlash = null; requestAnimationFrame(() => flashMaterial(tid)); }
}

function renderChrome(s) {
  const app = $('#app'), person = s.people[s.activePersonId] || null;
  const tier = person?.tier || 'gold';
  document.documentElement.dataset.arrowTier = tier;
  if (app) { app.dataset.viewMode = scale === 2 ? 'scale-months' : scale === 3 ? 'scale-years' : 'person-journal'; app.dataset.arrowTier = tier; app.dataset.journalEdition = s.edition || 'cotton'; }
  if ($('#folioSubject')) $('#folioSubject').textContent = person ? person.nameCn : '心络';
  if ($('#folioMonthBtn')) {
    $('#folioMonthBtn').hidden = !person;
    if ($('#folioMonthLabel')) $('#folioMonthLabel').textContent = scale === 3 ? '年轮 ▾' : scale === 2 ? '月册 ▾' : (activeMonthYm ? `${Number(activeMonthYm.slice(5, 7))}月 ▾` : '月册 ▾');
  }
  if ($('#searchToggleBtn')) { $('#searchToggleBtn').hidden = !person; $('#searchToggleBtn').classList.toggle('is-active', Boolean(s.searchOpen)); }
  if ($('#spotlightBar')) $('#spotlightBar').hidden = !s.searchOpen;
  $('#journalCanvas')?.classList.toggle('is-searching', Boolean(s.searchOpen));
  const qEntry = $('#questionEntry');
  if (qEntry) { const n = (s.questionCount || 0) + (s.inboxCount || 0); qEntry.hidden = !(n > 0); if (!qEntry.hidden) qEntry.textContent = `${n} 条待确认 ›`; }
}

export function renderJournal(s) {
  renderChrome(s);
  const person = s.people[s.activePersonId] || null;
  const arrowKey = person ? `${person.id}:${person.tier}` : '';
  if (arrowKey !== renderJournal.arrowKey) { renderJournal.arrowKey = arrowKey; renderArrow(person); }
  const mats = person ? s.materials[person.id] : null;
  const key = [person?.id || '', person?.tier || '', person?.nameCn || '', person?.roleTag || '', scale, selectedScale2Year || '', s.searchOpen, s.searchQuery, activeFacet, searchHits?.items?.length ?? -1, mats?.items?.length ?? -1, mats?.loaded, s.pending.length, isSilverVeiled(person?.id), (remindersCache.get(person?.id) || []).length].join('|');
  if (key === lastRenderKey) return;
  lastRenderKey = key;
  if (person) renderPage(s);
}

export function initJournal() {
  actions.stopActiveAudio = stopActiveAudio;
  if ($('#settingsBtn') && !$('#settingsBtn').children.length) $('#settingsBtn').append(svg(GEAR_SVG));
  if ($('#searchToggleBtn') && !$('#searchToggleBtn').children.length) $('#searchToggleBtn').append(svg(SEARCH_SVG));
  if ($('#spotlightIcon') && !$('#spotlightIcon').children.length) $('#spotlightIcon').append(svg(SEARCH_SVG));
  if ($('#spotlightClose') && !$('#spotlightClose').children.length) $('#spotlightClose').append(svg(CLOSE_SVG));

  $('#questionEntry')?.addEventListener('click', () => { haptic('light'); actions.openArchiveHistory?.({ filter: 'pending' }); });
  $('#folioSubjectBtn')?.addEventListener('click', () => { const p = store.get().people[store.get().activePersonId]; if (p) openVellumPocket(p, { onMutated: () => fetchReminders(p.id, { force: true }).then(() => renderJournal(store.get())) }); });
  $('#folioMonthBtn')?.addEventListener('click', () => setScale(scale === 1 ? 2 : scale === 2 ? 3 : 1));
  $('#searchToggleBtn')?.addEventListener('click', () => (store.get().searchOpen ? closeSearch() : openSearch()));
  $('#spotlightClose')?.addEventListener('click', () => { haptic('light'); closeSearch(); });
  $('#spotlightInput')?.addEventListener('input', (e) => {
    const q = e.target.value; store.set({ searchQuery: q });
    clearTimeout(searchDebounce);
    searchDebounce = setTimeout(() => { const pid = store.get().activePersonId; if (pid) runSemanticSearch(pid, q); }, 180);
  });
  $('#spotlightInput')?.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.preventDefault(); closeSearch(); } });

  const scroller = $('#journalScroll');
  scrubber = createGhostScrubber({ hostEl: $('#journalCanvas'), onJumpMonth: (t) => { activeMonthYm = t.ym; renderChrome(store.get()); }, onBackToLatest: () => scrollJournalToTop(true) });
  scroller?.addEventListener('scroll', () => {
    const top = scroller.scrollTop;
    $('#journalCanvas')?.classList.toggle('is-scrolled', top > 16);
    scrubber?.notifyScroll(top, activeMonthYm);
  }, { passive: true });
  if (scroller && typeof attachJournalPinch === 'function') {
    pinchAc?.abort(); pinchAc = new AbortController();
    attachJournalPinch({ scroller, virtualRiver: { setPinchHold: (v) => river?.setPinchHold?.(v) }, onZoomOut: () => setScale(scale + 1), onZoomIn: () => setScale(scale - 1), signal: pinchAc.signal });
  }
  store.subscribe(renderJournal);
  renderJournal(store.get());
}
