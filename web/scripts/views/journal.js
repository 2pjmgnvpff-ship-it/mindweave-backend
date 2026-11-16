// journal.js — one person's scrapbook page: dossier hero, masthead, materials (ink + pencil drafts
// in place), optimistic slips, empty-page prompt capsules and the question-notes entry.
import { h, clear, svg, $ } from '../dom.js';
import { api } from '../api/endpoints.js';
import { store, setMaterials, invalidateMaterials } from '../store.js';
import { personDecor, monogramOf, sealStyle } from '../decor.js';
import { TIER_LABEL, todayIso, formatDateCn } from '../format.js';
import { BRASS_CLIP_SVG, GEAR_SVG } from '../icons.js';
import { renderMaterial, renderPendingSlip, flashMaterial } from './journal-items.js';
import { renderArrow, strikeArrow } from './arrow.js';
import { openQuestionNotes } from './questions.js';
import { whisper } from './toast.js';
import { actions } from '../actions.js';

const PROMPTS = ['TA 有什么忌口？', '生日是哪天？', '最近有什么心愿？'];
const WEEK = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
const QUOTES = ['在意的人，值得被好好记住', '一句话就够，剩下的交给丘比特', '慢慢写，不着急', '今天也替你记着'];

let lastKey = '';
let inboxCache = new Map(); // personId → Promise<items>
let pendingFlash = null;

function mastHead() {
  const d = new Date();
  $('#hoboDay').textContent = String(d.getDate());
  $('#hoboMonthWk').textContent = `${d.getMonth() + 1}月 · ${WEEK[d.getDay()]}`;
  $('#hoboQuote').textContent = QUOTES[d.getDate() % QUOTES.length];
  $('#folioDate').textContent = formatDateCn(todayIso());
}

function inboxItem(id, personId) {
  if (!inboxCache.has(personId)) {
    inboxCache.set(
      personId,
      api.inbox({ personId }).then((r) => r.items || []).catch(() => []),
    );
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

/** Open a person's page (optionally flashing a material, e.g. from a chat citation). */
export function openPerson(personId, { materialId = null, fromDirectory = false } = {}) {
  const person = store.get().people[personId];
  if (!person) return;
  pendingFlash = materialId;
  store.set({ view: 'journal', activePersonId: personId, openedFromDirectory: fromDirectory || person.tier === 'crystal' });
  try {
    localStorage.setItem('mw.lastPerson', personId);
  } catch {
    /* private mode */
  }
  const m = store.get().materials[personId];
  if (!m || (!m.loaded && !m.loading)) loadMaterials(personId);
  $('#journalScroll').scrollTop = 0;
  strikeArrow();
}

function hero(person) {
  const d = personDecor(person);
  const tier = person.tier || 'crystal';
  const showPhoto = person.hasPhoto && person.mainPhotoUrl && tier !== 'silver';
  if (showPhoto) {
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
  const traits = (person.traits || []).filter(Boolean).slice(0, 4);
  return h(
    'div',
    { class: 'page-hero' },
    h(
      'section',
      { class: 'artifact-dossier-card', style: { '--rot': `${d.rotation}deg` }, 'aria-label': `${person.nameCn}的小档案` },
      h('span', { class: 'dossier-top-tape', 'aria-hidden': 'true', style: { background: d.tapeColor } }),
      h(
        'div',
        { class: 'dossier-card-head' },
        h('span', { class: ['dossier-monogram-seal', tier === 'silver' && 'is-cloaked'], style: tier === 'silver' ? {} : sealStyle(person), text: monogramOf(person), 'aria-hidden': 'true' }),
        h(
          'div',
          { class: 'dossier-head-meta' },
          h(
            'div',
            { class: 'dossier-name-line' },
            h('h1', { class: 'dossier-person-name', text: person.nameCn }),
            h('span', { class: 'dossier-role-pill', text: person.roleTag || TIER_LABEL[tier].short }),
          ),
          person.contextSub ? h('span', { class: 'dossier-context-sub', text: person.contextSub }) : null,
        ),
      ),
      h('div', { class: 'dossier-divider', 'aria-hidden': 'true' }),
      traits.length
        ? h('ul', { class: 'dossier-traits-list' }, ...traits.map((t) => h('li', { text: typeof t === 'string' ? t : t.text || '' })))
        : h('p', { class: 'dossier-empty-line', text: person.shortMemo || '关于 TA 的小事，会慢慢长在这一页。' }),
    ),
  );
}

function promptCapsules(person) {
  return h(
    'div',
    { class: 'prompt-capsules' },
    h('p', { class: 'prompt-intro', text: `这一页还空着。想到「${person.nameCn}」的什么，说一句就好：` }),
    ...PROMPTS.map((p) =>
      h(
        'button',
        { type: 'button', class: 'prompt-capsule', onClick: () => actions.openTextCapture({ personId: person.id, prompt: p }) },
        h('span', { class: 'cap-dot', 'aria-hidden': 'true', text: '✎' }),
        h('span', { text: p }),
      ),
    ),
  );
}

function renderPage(s) {
  const scroll = $('#journalScroll');
  const person = s.people[s.activePersonId];
  clear(scroll);
  if (!person) return;
  const mats = s.materials[person.id];
  const pending = s.pending.filter((p) => p.pageId === person.id);
  const flow = h('div', { class: 'page-flow' });
  for (const p of pending) flow.appendChild(renderPendingSlip(p));
  const items = mats?.items || [];
  for (const m of items) flow.appendChild(renderMaterial(m, person, { inboxItem }));
  if (mats?.loaded && !items.length && !pending.length) flow.appendChild(promptCapsules(person));
  else if (!mats?.loaded) flow.appendChild(h('p', { class: 'page-end-mark', text: '正在翻开…' }));
  if (mats?.hasMore) {
    flow.appendChild(h('button', { type: 'button', class: 'load-more-btn', text: '再往前翻翻', onClick: () => loadMaterials(person.id, { append: true }) }));
  } else if (items.length > 2) {
    flow.appendChild(h('p', { class: 'page-end-mark', 'aria-hidden': 'true', text: '· · ·' }));
  }
  scroll.append(hero(person), flow);
  if (pendingFlash && items.length) {
    const id = pendingFlash;
    pendingFlash = null;
    requestAnimationFrame(() => {
      if (!flashMaterial(id)) whisper('那一页可能被翻到更早的地方了', 2400);
    });
  }
}

function renderChrome(s) {
  const app = $('#app');
  const person = s.view === 'journal' ? s.people[s.activePersonId] : null;
  app.dataset.viewMode = s.view === 'directory' ? 'casual-directory' : 'person-journal';
  app.dataset.arrowTier = person?.tier || 'crystal';
  app.dataset.journalEdition = s.edition;
  $('#folioSubject').textContent = s.view === 'directory' ? '记繁琐' : person ? `${person.nameCn} · ${TIER_LABEL[person.tier || 'crystal'].short}` : '';
  $('#backToCasual').hidden = !(s.view === 'journal' && person && person.tier === 'crystal');
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
    ? [person.id, person.tier, person.nameCn, person.version, s.materials[person.id], s.pending.filter((p) => p.pageId === person.id).map((p) => `${p.id}:${p.state}`).join(',')]
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

// stable identity for objects (materials page) so we only re-render on real changes
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
  $('#backToCasual').addEventListener('click', () => actions.openDirectory());
  $('#questionEntry').addEventListener('click', () => openQuestionNotes());
  store.subscribe(renderJournal);
  renderJournal(store.get());
}
