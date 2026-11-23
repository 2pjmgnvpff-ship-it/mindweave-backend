// journal.js — organic editorial scrapbook page: month chapters with asymmetric collage flow,
// right-edge physical paper index tabs, ultra-minimal top bar (Cover Vellum Pocket Note,
// Month Book Pages Grid, Spotlight Search), and empty-page prompt capsules.
import { h, clear, svg, $ } from '../dom.js';
import { api } from '../api/endpoints.js';
import { store, setMaterials, invalidateMaterials } from '../store.js';
import { isShortNoteSlip, gentleTilt, monogramOf, sealStyle } from '../decor.js';
import { TIER_LABEL, todayIso, formatDateCn, clip, groupMaterialsByMonth, matchesMaterialQuery, buildVellumSections } from '../format.js';
import { BRASS_CLIP_SVG, GEAR_SVG, SEARCH_SVG, CLOSE_SVG } from '../icons.js';
import { renderMaterial, renderPendingSlip, flashMaterial, closePeek } from './journal-items.js';
import { renderArrow, strikeArrow } from './arrow.js';
import { openQuestionNotes } from './questions.js';
import { openSheet, closeSheet } from './sheet.js';
import { whisper } from './toast.js';
import { actions } from '../actions.js';

const PROMPTS = ['TA 有什么忌口？', '生日是哪天？', '最近有什么心愿？'];
const WEEK = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
const QUOTES = ['在意的人，值得被好好记住', '一句话就够，剩下的交给丘比特', '慢慢写，不着急', '今天也替你记着'];

let lastKey = '';
let inboxCache = new Map();
let pendingFlash = null;
let currentChapters = [];
let activeMonthYm = '';
let scrollRaf = 0;

function mastHead() {
  const d = new Date();
  $('#hoboDay').textContent = String(d.getDate());
  $('#hoboMonthWk').textContent = `${d.getMonth() + 1}月 · ${WEEK[d.getDay()]}`;
  $('#hoboQuote').textContent = QUOTES[d.getDate() % QUOTES.length];
  $('#folioDate').textContent = formatDateCn(todayIso());
}

function inboxItem(id, personId) {
  if (!inboxCache.has(personId)) {
    inboxCache.set(personId, api.inbox({ personId }).then((r) => r.items || []).catch(() => []));
  }
  return inboxCache.get(personId).then((items) => items.find((i) => i.id === id) || null);
}

export async function loadMaterials(personId, { append = false } = {}) {
  if (!personId) return;
  const s = store.get();
  const prev = s.materials[personId];
  if (prev?.loading) return;
  store.set({ materials: { ...s.materials, [personId]: { items: prev?.items || [], hasMore: false, nextCursor: null, ...prev, loading: true } } });
  inboxCache.delete(personId);
  try {
    const page = await api.materials(personId, { cursor: append ? prev?.nextCursor : undefined, limit: 30 });
    setMaterials(personId, page, { append });
  } catch (err) {
    const cur = store.get().materials;
    store.set({ materials: { ...cur, [personId]: { ...(cur[personId] || {}), items: cur[personId]?.items || [], loading: false, loaded: true, failed: true } } });
    whisper(err.message, 3000);
  }
}

export function reloadMaterials(personId) {
  invalidateMaterials(personId);
  if (store.get().activePersonId === personId) loadMaterials(personId);
}

export function openPerson(personId, { materialId = null, fromDirectory = false } = {}) {
  const prev = store.get();
  const person = prev.people[personId];
  if (!person) return;
  closePeek();
  const samePage = prev.view === 'journal' && prev.activePersonId === personId && prev.journalFilter === 'all' && !prev.searchOpen;
  pendingFlash = materialId;
  const inp = $('#spotlightInput');
  if (inp) inp.value = '';
  store.set({
    view: 'journal',
    activePersonId: personId,
    openedFromDirectory: fromDirectory || person.tier === 'crystal',
    journalFilter: 'all',
    searchOpen: false,
    searchQuery: '',
  });
  try { localStorage.setItem('mw.lastPerson', personId); } catch { /* private mode */ }
  const m = store.get().materials[personId];
  if (!m || (!m.loaded && !m.loading)) loadMaterials(personId);
  else if (samePage && materialId) {
    pendingFlash = null;
    requestAnimationFrame(() => { if (!flashMaterial(materialId)) whisper('那一页可能被翻到更早的地方了', 2400); });
  }
  if (!materialId) $('#journalScroll').scrollTop = 0;
  strikeArrow();
}

/* ---------------- Organic Asymmetric Editorial Collage ---------------- */
function renderChapter(ch, person, query) {
  const tier = person.tier || 'crystal';
  const collage = h('div', { class: 'month-collage' });
  const list = ch.items;
  let soloIdx = 0;
  for (let i = 0; i < list.length; ) {
    if (isShortNoteSlip(list[i], tier) && i + 1 < list.length && isShortNoteSlip(list[i + 1], tier)) {
      const leftCard = renderMaterial(list[i], person, { inboxItem, query, layout: 'pair-left', side: 'left', rot: gentleTilt(list[i], tier, -1) });
      const rightCard = renderMaterial(list[i + 1], person, { inboxItem, query, layout: 'pair-right', side: 'right', rot: gentleTilt(list[i + 1], tier, 1) });
      collage.appendChild(h('div', { class: 'collage-pair' }, leftCard, rightCard));
      i += 2;
      continue;
    }
    const m = list[i];
    const len = Array.from(String(m.body || m.snippet || '').trim()).length;
    const isMedia = (m.type === 'photo' && m.mediaUrl && tier !== 'silver') || m.type === 'voice';
    const layout = len > 62 || isMedia || m.presentation === 'question' ? 'wide' : 'medium';
    const side = soloIdx % 2 === 0 ? 'left' : 'right';
    collage.appendChild(renderMaterial(m, person, { inboxItem, query, layout, side, rot: gentleTilt(m, tier, side === 'left' ? -1 : 1) }));
    soloIdx += 1;
    i += 1;
  }
  return h(
    'section',
    { class: 'month-chapter', dataset: { month: ch.ym }, 'aria-label': ch.label },
    h('header', { class: 'month-header' }, h('h2', { class: 'month-watermark', text: ch.label }), h('span', { class: 'month-hairline', 'aria-hidden': 'true' })),
    collage,
  );
}

function photoHero(person) {
  const tier = person.tier || 'crystal';
  if (!(person.hasPhoto && person.mainPhotoUrl && tier !== 'silver')) return null;
  return h(
    'div',
    { class: 'page-hero' },
    h(
      'figure',
      { class: 'artifact-photo-mount' },
      h('span', { class: 'mount-hw hw-brass-clip' }, svg(BRASS_CLIP_SVG)),
      h('span', { class: 'mount-hw hw-enamel-pin' }),
      h('span', { class: 'film-sprocket-rail left' }),
      h('span', { class: 'film-sprocket-rail right' }),
      h('div', { class: 'photo-slot-primary' }, h('img', { src: person.mainPhotoUrl, alt: `${person.nameCn}的照片`, loading: 'lazy' })),
      h('figcaption', { class: 'polaroid-hand-caption', text: person.contextSub || person.nameCn }),
    ),
  );
}

function promptCapsules(person) {
  return h(
    'div',
    { class: 'prompt-capsules' },
    h('p', { class: 'prompt-intro', text: `这一页还空着。想到「${person.nameCn}」的什么，说一句就好：` }),
    ...PROMPTS.map((p) =>
      h('button', { type: 'button', class: 'prompt-capsule', onClick: () => actions.openTextCapture({ personId: person.id, prompt: p }) }, h('span', { class: 'cap-dot', 'aria-hidden': 'true', text: '✎' }), h('span', { text: p })),
    ),
  );
}

/* ---------------- Right-Edge Physical Paper Index Tabs ---------------- */
function scrollToMonth(ym) {
  const scroll = $('#journalScroll');
  const el = scroll?.querySelector(`.month-chapter[data-month="${CSS.escape(ym)}"]`);
  if (!scroll || !el) return;
  const top = el.getBoundingClientRect().top - scroll.getBoundingClientRect().top + scroll.scrollTop - 102;
  scroll.scrollTo({ top: Math.max(0, top), behavior: 'smooth' });
  setActiveMonthUi(ym);
}

function setActiveMonthUi(ym) {
  if (!ym) return;
  activeMonthYm = ym;
  const ch = currentChapters.find((c) => c.ym === ym);
  if (ch && $('#folioMonthLabel')) $('#folioMonthLabel').textContent = ch.label;
  const pinnedMode = store.get().journalFilter === 'pinned';
  for (const btn of $('#paperEdgeTabs')?.querySelectorAll('.paper-edge-tab[data-month]') || []) {
    btn.classList.toggle('is-active', !pinnedMode && btn.dataset.month === ym);
  }
}

function syncActiveMonthFromScroll() {
  const scroll = $('#journalScroll');
  if (!scroll || store.get().view !== 'journal') return;
  const chapters = scroll.querySelectorAll('.month-chapter[data-month]');
  if (!chapters.length) return;
  const topLine = scroll.getBoundingClientRect().top + 115;
  let picked = chapters[0].dataset.month;
  for (const ch of chapters) {
    if (ch.getBoundingClientRect().top <= topLine) picked = ch.dataset.month;
    else break;
  }
  if (picked && picked !== activeMonthYm) setActiveMonthUi(picked);
}

function renderPaperTabs(s, allItems, allChapters) {
  const host = $('#paperEdgeTabs');
  const pinnedCount = allItems.filter((m) => m.pinned && m.presentation !== 'question').length;
  const show = s.view === 'journal' && !s.searchOpen && allItems.length >= 2 && (allChapters.length >= 2 || pinnedCount > 0);
  host.hidden = !show;
  $('#journalCanvas').classList.toggle('has-edge-tabs', show);
  clear(host);
  if (!show) return;

  if (pinnedCount > 0) {
    const isPin = s.journalFilter === 'pinned';
    host.appendChild(
      h(
        'button',
        {
          type: 'button',
          class: ['paper-edge-tab', 'is-pin-tab', isPin && 'is-active'],
          'aria-pressed': String(isPin),
          onClick: () => { store.set({ journalFilter: isPin ? 'all' : 'pinned' }); $('#journalScroll').scrollTop = 0; },
        },
        h('span', { class: 'tab-brass-dot', 'aria-hidden': 'true' }),
        h('span', { text: '置顶' }),
      ),
    );
  }
  for (const ch of allChapters) {
    const isAct = s.journalFilter !== 'pinned' && (activeMonthYm ? activeMonthYm === ch.ym : ch === allChapters[0]);
    host.appendChild(
      h('button', {
        type: 'button',
        class: ['paper-edge-tab', isAct && 'is-active'],
        dataset: { month: ch.ym },
        text: ch.shortLabel,
        onClick: () => {
          if (store.get().journalFilter === 'pinned') {
            store.set({ journalFilter: 'all' });
            requestAnimationFrame(() => scrollToMonth(ch.ym));
          } else scrollToMonth(ch.ym);
        },
      }),
    );
  }
}

/* ---------------- Cover Vellum Pocket Note & Month Book Grid ---------------- */
function jumpToRecord(recordId) {
  closeSheet();
  if (!recordId) return;
  const s = store.get();
  if (s.journalFilter !== 'all' || s.searchOpen) {
    closeSearch();
    store.set({ journalFilter: 'all' });
    pendingFlash = recordId;
    return;
  }
  requestAnimationFrame(() => { if (!flashMaterial(recordId)) whisper('这条记录在更早的页里，再往前翻翻就能看到', 2600); });
}

async function openVellumPocket(person) {
  if (!person) return;
  const tier = person.tier || 'crystal';
  const content = h('div', { class: 'vellum-loading', text: '正在展开手帐衬纸…' });
  const header = h(
    'header',
    { class: 'vellum-head' },
    h('span', { class: ['mono-seal', tier === 'silver' && 'is-cloaked'], style: tier === 'silver' ? {} : sealStyle(person), text: monogramOf(person), 'aria-hidden': 'true' }),
    h('div', { class: 'vellum-head-meta' }, h('h2', { class: 'vellum-name', text: person.nameCn }), h('p', { class: 'vellum-tier-whisper', text: person.roleTag ? `${person.roleTag} · ${TIER_LABEL[tier].short}` : `${TIER_LABEL[tier].arrow} · ${TIER_LABEL[tier].short}` })),
  );
  openSheet({ label: `${person.nameCn}的手帐衬纸`, body: h('div', { class: ['vellum-pocket', `tier-${tier}`] }, header, content) });

  const [factsRes, remRes] = await Promise.all([
    api.facts(person.id, { limit: 100 }).catch(() => ({ items: [] })),
    api.reminders(person.id).catch(() => ({ items: [] })),
  ]);
  if (!content.isConnected) return;
  clear(content);

  const { dateLines, detailLines } = buildVellumSections(factsRes?.items, remRes?.items);
  if (!dateLines.length && !detailLines.length) {
    content.appendChild(h('p', { class: 'vellum-empty', text: `关于「${person.nameCn}」的日子与小事，会慢慢浮现在这张衬纸上。` }));
    return;
  }
  const row = ({ when, text, recordId, pending }) =>
    h(
      'button',
      { type: 'button', class: ['vellum-line', pending && 'is-pending', !recordId && 'is-static'], onClick: () => jumpToRecord(recordId) },
      when ? h('span', { class: 'vellum-line-when', text: when }) : h('span', { class: 'vellum-line-dot', 'aria-hidden': 'true' }),
      h('span', { class: 'vellum-line-text', text }),
    );
  if (dateLines.length) content.appendChild(h('section', { class: 'vellum-section' }, h('h3', { class: 'vellum-sec-title', text: '记挂的日子' }), ...dateLines.map(row)));
  if (detailLines.length) content.appendChild(h('section', { class: 'vellum-section' }, h('h3', { class: 'vellum-sec-title', text: tier === 'silver' ? '边界与留痕' : '记住的小事' }), ...detailLines.map(row)));
}

function openMonthBookGrid(person) {
  if (!person || currentChapters.length < 2) return;
  const grid = h(
    'div',
    { class: 'month-book-grid' },
    ...currentChapters.map((ch) =>
      h(
        'button',
        {
          type: 'button',
          class: ['month-page-card', activeMonthYm === ch.ym && 'is-current'],
          onClick: () => {
            closeSheet();
            if (store.get().journalFilter === 'pinned') {
              store.set({ journalFilter: 'all' });
              requestAnimationFrame(() => scrollToMonth(ch.ym));
            } else scrollToMonth(ch.ym);
          },
        },
        h('div', { class: 'month-page-top' }, h('strong', { text: ch.label }), h('span', { text: `${ch.items.length} 页` })),
        h('p', { class: 'month-page-excerpt', text: clip(ch.items[0]?.body || ch.items[0]?.snippet || '', 34) || '几句随手记' }),
      ),
    ),
  );
  openSheet({ title: '翻到哪一月', sub: `${person.nameCn}的手帐 · 共 ${currentChapters.length} 个月份`, body: grid });
}

/* ---------------- Spotlight Search ---------------- */
function openSearch() {
  store.set({ searchOpen: true });
  requestAnimationFrame(() => $('#spotlightInput')?.focus({ preventScroll: true }));
}

function closeSearch() {
  const inp = $('#spotlightInput');
  if (inp) inp.value = '';
  store.set({ searchOpen: false, searchQuery: '' });
}

/* ---------------- Page & Chrome Rendering ---------------- */
function renderPage(s) {
  const scroll = $('#journalScroll');
  const person = s.people[s.activePersonId];
  clear(scroll);
  if (!person) return;

  const mats = s.materials[person.id];
  const allItems = mats?.items || [];
  const pending = s.pending.filter((p) => p.pageId === person.id);
  const query = s.searchOpen ? s.searchQuery.trim() : '';

  const allChapters = groupMaterialsByMonth(allItems);
  currentChapters = allChapters;
  if (!activeMonthYm || !allChapters.some((c) => c.ym === activeMonthYm)) activeMonthYm = allChapters[0]?.ym || '';

  let visible = allItems;
  if (query) visible = allItems.filter((m) => matchesMaterialQuery(m, query));
  else if (s.journalFilter === 'pinned') visible = allItems.filter((m) => m.pinned && m.presentation !== 'question');

  const countEl = $('#spotlightCount');
  if (countEl) {
    countEl.hidden = !query;
    if (query) countEl.textContent = `${visible.length} 处`;
  }

  const flow = h('div', { class: 'page-flow' });
  if (s.journalFilter === 'pinned' && !query) {
    flow.appendChild(h('div', { class: 'pinned-filter-whisper' }, h('span', { text: `铜钉留存 · ${visible.length} 页` }), h('button', { type: 'button', class: 'filter-reset-link', text: '看全部', onClick: () => store.set({ journalFilter: 'all' }) })));
  }
  if (!query && s.journalFilter === 'all') for (const p of pending) flow.appendChild(renderPendingSlip(p));

  for (const ch of groupMaterialsByMonth(visible)) flow.appendChild(renderChapter(ch, person, query));

  if (query && !visible.length) {
    flow.appendChild(h('div', { class: 'search-empty-whisper' }, h('p', { text: `这一页里暂时没找到「${query}」` }), mats?.hasMore ? h('button', { type: 'button', class: 'load-more-btn', text: '再往前翻翻找找', onClick: () => loadMaterials(person.id, { append: true }) }) : null));
  } else if (s.journalFilter === 'pinned' && !visible.length) {
    flow.appendChild(h('p', { class: 'page-end-mark', text: '还没有用铜钉留下的记录' }));
  } else if (mats?.loaded && !allItems.length && !pending.length) {
    flow.appendChild(promptCapsules(person));
  } else if (!mats?.loaded) {
    flow.appendChild(h('p', { class: 'page-end-mark', text: '正在翻开…' }));
  }

  if (mats?.hasMore && !query) {
    flow.appendChild(h('button', { type: 'button', class: 'load-more-btn', text: '再往前翻翻', onClick: () => loadMaterials(person.id, { append: true }) }));
  } else if (visible.length > 2) {
    flow.appendChild(h('p', { class: 'page-end-mark', 'aria-hidden': 'true', text: '· · ·' }));
  }

  const heroEl = !query && s.journalFilter === 'all' ? photoHero(person) : null;
  if (heroEl) scroll.append(heroEl, flow);
  else scroll.append(flow);

  renderPaperTabs(s, allItems, allChapters);
  const showMonth = s.view === 'journal' && !s.searchOpen && allChapters.length >= 2;
  $('#folioMonthBtn').hidden = !showMonth;
  if (showMonth) $('#folioMonthLabel').textContent = (allChapters.find((c) => c.ym === activeMonthYm) || allChapters[0]).label;

  if (pendingFlash && allItems.length) {
    const id = pendingFlash;
    pendingFlash = null;
    requestAnimationFrame(() => { if (!flashMaterial(id)) whisper('那一页可能被翻到更早的地方了', 2400); });
  }
}

function renderChrome(s) {
  const app = $('#app');
  const person = s.view === 'journal' ? s.people[s.activePersonId] : null;
  const items = person ? s.materials[person.id]?.items || [] : [];
  app.dataset.viewMode = s.view === 'directory' ? 'casual-directory' : 'person-journal';
  app.dataset.arrowTier = person?.tier || 'crystal';
  app.dataset.journalEdition = s.edition;

  $('#folioSubject').textContent = s.view === 'directory' ? '记繁琐' : person ? person.nameCn : '';
  const subjBtn = $('#folioSubjectBtn');
  subjBtn.disabled = !person;
  subjBtn.classList.toggle('is-interactive', Boolean(person));
  $('#backToCasual').hidden = !(s.view === 'journal' && person && person.tier === 'crystal');

  const searchBtn = $('#searchToggleBtn');
  searchBtn.hidden = !(s.view === 'journal' && person && items.length > 0);
  searchBtn.classList.toggle('is-active', Boolean(s.searchOpen));

  const spotVisible = Boolean(s.searchOpen && s.view === 'journal' && person);
  $('#spotlightBar').hidden = !spotVisible;
  $('#journalCanvas').classList.toggle('is-searching', spotVisible);
  if (s.view !== 'journal') {
    $('#folioMonthBtn').hidden = true;
    $('#paperEdgeTabs').hidden = true;
    $('#journalCanvas').classList.remove('has-edge-tabs');
  }

  const q = $('#questionEntry');
  q.hidden = !(s.questionCount > 0);
  if (!q.hidden) {
    clear(q);
    q.append(h('b', { text: String(s.questionCount) }), h('span', { text: '张便签等你看一眼' }));
  }
}

export function renderJournal(s) {
  renderChrome(s);
  const person = s.view === 'journal' ? s.people[s.activePersonId] : null;
  const key = person
    ? [person.id, person.tier, person.nameCn, person.version, s.journalFilter, s.searchOpen, s.searchQuery, s.materials[person.id], s.pending.filter((p) => p.pageId === person.id).map((p) => `${p.id}:${p.state}`).join(',')]
    : ['none'];
  const arrowKey = person ? `${person.id}:${person.tier}` : '';
  if (arrowKey !== renderJournal.arrowKey) {
    renderJournal.arrowKey = arrowKey;
    renderArrow(person);
  }
  const k = key.map((x) => (typeof x === 'object' ? identity(x) : String(x))).join('|');
  if (k === lastKey) return;
  lastKey = k;
  if (person) renderPage(s);
}

const ids = new WeakMap();
let seq = 0;
function identity(obj) {
  if (!obj) return 'null';
  if (!ids.has(obj)) ids.set(obj, ++seq);
  return `o${ids.get(obj)}`;
}

export function initJournal() {
  mastHead();
  $('#settingsBtn').append(svg(GEAR_SVG));
  $('#searchToggleBtn').append(svg(SEARCH_SVG));
  $('#spotlightIcon').append(svg(SEARCH_SVG));
  $('#spotlightClose').append(svg(CLOSE_SVG));

  $('#backToCasual').addEventListener('click', () => { closePeek(); closeSearch(); actions.openDirectory(); });
  $('#questionEntry').addEventListener('click', () => openQuestionNotes());
  $('#folioSubjectBtn').addEventListener('click', () => { const s = store.get(); if (s.view === 'journal') openVellumPocket(s.people[s.activePersonId]); });
  $('#folioMonthBtn').addEventListener('click', () => { const s = store.get(); if (s.view === 'journal') openMonthBookGrid(s.people[s.activePersonId]); });
  $('#searchToggleBtn').addEventListener('click', () => (store.get().searchOpen ? closeSearch() : openSearch()));
  $('#spotlightClose').addEventListener('click', () => closeSearch());
  $('#spotlightInput').addEventListener('input', (e) => store.set({ searchQuery: e.target.value }));
  $('#spotlightInput').addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.preventDefault(); closeSearch(); } });
  $('#journalScroll').addEventListener('scroll', () => {
    if (scrollRaf) return;
    scrollRaf = requestAnimationFrame(() => { scrollRaf = 0; syncActiveMonthFromScroll(); });
  }, { passive: true });

  store.subscribe(renderJournal);
  renderJournal(store.get());
}
