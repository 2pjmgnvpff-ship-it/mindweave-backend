// chat-drawer.js — 324px zero-blur Right-Edge Memory Drawer for Cupid Chat (01e / 03e / G08).
// Features: 1:1 finger-tracked swipe via swipe-arbiter.js (0 parent scale, display:none when closed),
// collapsible 5-Min Pre-Contact Brief, 180ms B1 /search, 3 quick pills, O(1) classList selection,
// sticky bottom CTA, and strict Silver zero-photo guard.
import { h, clear } from '../dom.js';
import { api } from '../api/endpoints.js';
import { store } from '../store.js';
import { matchesMaterialQuery } from '../format.js';
import { haptic } from '../native-bridge.js';
import { attachHorizontalDrawer } from '../lib/swipe-arbiter.js';
import { drawerCard } from './chat-render.js';
import { loadMaterials } from './journal.js';

const CLOSE_HIDE_MS = 220;
const SEARCH_DEBOUNCE_MS = 180;

export function createChatDrawer({ root, getPerson, getAttached, onToggleAttach, onApplySelection, onOpenPeek }) {
  let isOpen = false;
  let filterMode = 'all'; // 'all' | 'selected' | 'pinned'
  let query = '';
  let searchResults = null;
  let searchSeq = 0;
  let searchTimer = null;
  let closeTimer = null;
  let briefPersonId = null;
  let drawerAbort = new AbortController();
  const cardById = new Map();

  const scrim = h('div', { class: 'chat-material-scrim', hidden: true, onClick: () => setOpen(false) });
  const countPill = h('span', { class: 'drawer-count-pill', text: '0' });
  const briefHost = h(
    'details',
    { class: 'brief-card-5min', hidden: true, style: { display: 'none' } },
    h('summary', { class: 'brief-5min-summary', text: '见面 5 分钟速览' }),
  );
  const searchInput = h('input', {
    type: 'search',
    class: 'mw-sheet-search drawer-search-input',
    placeholder: '翻找手帐里的线索…',
    'aria-label': '搜索手帐',
    autocomplete: 'off',
  });
  const searchClear = h('button', {
    type: 'button',
    class: 'drawer-search-clear',
    'aria-label': '清空搜索',
    text: '✕',
    hidden: true,
    style: { display: 'none' },
    onClick: () => {
      haptic('light');
      resetSearch();
      renderList();
      searchInput.focus({ preventScroll: true });
    },
  });
  const pillAll = h('button', { type: 'button', class: 'drawer-filter-pill is-active', text: '全部', onClick: () => setFilter('all') });
  const pillSel = h('button', { type: 'button', class: 'drawer-filter-pill', text: '已选 0', hidden: true, style: { display: 'none' }, onClick: () => setFilter('selected') });
  const pillPin = h('button', { type: 'button', class: 'drawer-filter-pill', text: '置顶', onClick: () => setFilter('pinned') });
  const pillsBar = h('div', { class: 'drawer-filter-bar', role: 'tablist', 'aria-label': '筛选手帐' }, pillAll, pillSel, pillPin);
  const listEl = h('div', { class: 'drawer-materials-list' });
  const stickyCta = h('button', {
    type: 'button',
    class: 'drawer-sticky-cta',
    onClick: () => {
      haptic('light');
      setOpen(false, { restoreFocus: false });
      onApplySelection?.();
    },
  });
  const drawerFoot = h('div', { class: 'drawer-foot', hidden: true }, stickyCta);

  const drawer = h(
    'aside',
    { class: 'chat-material-drawer', 'aria-label': '挑几页手帐给丘比特看', 'aria-hidden': 'true', hidden: true },
    h(
      'div',
      { class: 'drawer-inner-header' },
      h('div', {}, h('p', { class: 'drawer-title', text: '挑几页给丘比特看' }), h('p', { class: 'drawer-subtitle', text: '选中的会随下一句一起参考' })),
      countPill,
    ),
    briefHost,
    h('div', { class: 'drawer-search-wrap' }, searchInput, searchClear),
    pillsBar,
    listEl,
    drawerFoot,
  );

  function syncSelectionCounters() {
    const n = getAttached().size;
    countPill.textContent = String(n);
    pillSel.textContent = `已选 ${n}`;
    pillSel.hidden = n === 0;
    pillSel.style.display = n === 0 ? 'none' : '';
    if (n === 0 && filterMode === 'selected') {
      filterMode = 'all';
      syncFilterPills();
    }
    drawerFoot.hidden = n === 0;
    stickyCta.textContent = `带上这 ${n} 页去聊 →`;
  }

  function syncFilterPills() {
    pillAll.classList.toggle('is-active', filterMode === 'all');
    pillSel.classList.toggle('is-active', filterMode === 'selected');
    pillPin.classList.toggle('is-active', filterMode === 'pinned');
  }

  function setFilter(next) {
    if (filterMode === next) return;
    haptic('selection');
    filterMode = next;
    syncFilterPills();
    renderList();
  }

  function resetSearch() {
    query = '';
    searchResults = null;
    searchSeq += 1;
    clearTimeout(searchTimer);
    searchInput.value = '';
    searchClear.hidden = true;
    searchClear.style.display = 'none';
  }

  /** O(1) visual toggle without wiping or rebuilding DOM nodes. */
  function syncCardSelection(materialId, selected) {
    const card = cardById.get(materialId);
    if (card) {
      card.classList.toggle('is-selected', selected);
      card.setAttribute('aria-pressed', String(selected));
    }
    syncSelectionCounters();
    if (filterMode === 'selected' && !selected) renderList();
  }

  function renderBrief(brief) {
    clear(briefHost);
    const headline = String(brief?.headline || '').trim();
    if (!headline) {
      briefHost.hidden = true;
      briefHost.style.display = 'none';
      return;
    }
    const subline = String(brief?.subline || '').trim();
    const bullets = Array.isArray(brief?.bullets) ? brief.bullets.slice(0, 3) : [];
    briefHost.append(
      h(
        'summary',
        { class: 'brief-5min-summary', onClick: () => haptic('selection') },
        h('span', { class: 'brief-5min-kicker', text: '见面五分钟速览' }),
        h('span', { class: 'brief-5min-headline', text: headline }),
      ),
      h(
        'div',
        { class: 'brief-5min-body' },
        subline ? h('p', { class: 'brief-5min-sub', text: subline }) : null,
        bullets.length ? h('ul', { class: 'brief-5min-list' }, ...bullets.map((b) => h('li', { text: String(b) }))) : null,
      ),
    );
    briefHost.hidden = false;
    briefHost.style.display = '';
  }

  async function ensureBrief(personId) {
    if (!personId || briefPersonId === personId || typeof api.brief !== 'function') return;
    briefPersonId = personId;
    try {
      const brief = await api.brief(personId);
      if (getPerson()?.id === personId) renderBrief(brief);
    } catch {
      briefHost.hidden = true;
      briefHost.style.display = 'none';
    }
  }

  function renderList() {
    const p = getPerson();
    const pid = p?.id;
    const tier = p?.tier || 'crystal';
    const allItems = store.get().materials[pid]?.items || [];
    const attached = getAttached();
    const base = query && Array.isArray(searchResults) ? searchResults : query ? allItems.filter((m) => matchesMaterialQuery(m, query)) : allItems;
    const items =
      filterMode === 'selected'
        ? base.filter((m) => attached.has(m.id))
        : filterMode === 'pinned'
          ? base.filter((m) => Boolean(m.pinned))
          : base;

    clear(listEl);
    cardById.clear();
    syncSelectionCounters();

    if (!allItems.length) {
      listEl.appendChild(h('p', { class: 'drawer-empty', text: '这一页还没有手帐，先回去记一句吧' }));
      return;
    }
    if (!items.length) {
      listEl.appendChild(h('p', { class: 'drawer-empty', text: query ? '没有找到相关的手帐，换个词试试' : '当前筛选下没有内容' }));
      return;
    }

    const frag = document.createDocumentFragment();
    for (const m of items) {
      const card = drawerCard(m, {
        selected: attached.has(m.id),
        tier,
        onToggle: () => {
          const nextSelected = onToggleAttach(m.id);
          if (typeof nextSelected === 'boolean') syncCardSelection(m.id, nextSelected);
        },
        onPeek: () => onOpenPeek?.(m.id),
      });
      cardById.set(m.id, card);
      frag.appendChild(card);
    }
    listEl.appendChild(frag);
  }

  searchInput.addEventListener('input', () => {
    query = searchInput.value.trim();
    searchClear.hidden = !query;
    clearTimeout(searchTimer);
    if (!query) {
      searchResults = null;
      renderList();
      return;
    }
    renderList();
    const pid = getPerson()?.id;
    if (!pid || typeof api.personSearch !== 'function') return;
    const seq = ++searchSeq;
    searchTimer = setTimeout(async () => {
      try {
        const res = await api.personSearch(pid, { q: query, pinned: filterMode === 'pinned' ? true : undefined });
        if (seq !== searchSeq || getPerson()?.id !== pid) return;
        const raw = Array.isArray(res?.items) ? res.items : null;
        searchResults = raw
          ? raw.map((hit) => (hit?.material ? { ...hit.material, matchKind: hit.matchKind, matchedTerms: hit.matchedTerms } : hit))
          : null;
        renderList();
      } catch {
        /* local filter fallback stays visible */
      }
    }, SEARCH_DEBOUNCE_MS);
  });

  function setOpen(open, { restoreFocus = true } = {}) {
    if (isOpen === open && !closeTimer) return;
    clearTimeout(closeTimer);
    closeTimer = null;
    isOpen = Boolean(open);
    if (isOpen) {
      drawer.hidden = false;
      scrim.hidden = false;
      drawer.style.display = '';
      scrim.style.display = '';
      const pid = getPerson()?.id;
      if (pid && !store.get().materials[pid]?.loaded) {
        loadMaterials(pid).then(() => getPerson()?.id === pid && isOpen && renderList());
      }
      ensureBrief(pid);
      renderList();
      requestAnimationFrame(() => {
        drawer.classList.add('is-open');
        scrim.classList.add('is-open');
      });
      drawer.setAttribute('aria-hidden', 'false');
    } else {
      drawer.classList.remove('is-open');
      scrim.classList.remove('is-open');
      drawer.setAttribute('aria-hidden', 'true');
      resetSearch();
      closeTimer = setTimeout(() => {
        closeTimer = null;
        if (!isOpen) {
          drawer.hidden = true;
          scrim.hidden = true;
          drawer.style.display = 'none';
          scrim.style.display = 'none';
        }
      }, CLOSE_HIDE_MS);
    }
    if (!open && restoreFocus) root?.querySelector?.('.muse-drawer-pill')?.focus?.({ preventScroll: true });
  }

  // Bind 1:1 finger-tracked swipe arbiter (0 parent scale, edgeGuardPx: 24)
  attachHorizontalDrawer?.({
    surface: drawer,
    scrim,
    triggerArea: root,
    widthPx: 324,
    edgeGuardPx: 24,
    onOpen: () => {
      haptic('light');
      setOpen(true);
    },
    onClose: () => {
      haptic('light');
      setOpen(false, { restoreFocus: false });
    },
    signal: drawerAbort.signal,
  });

  function resetPerson() {
    briefPersonId = null;
    briefHost.hidden = true;
    clear(briefHost);
    filterMode = 'all';
    syncFilterPills();
    resetSearch();
    syncSelectionCounters();
  }

  function destroy() {
    clearTimeout(searchTimer);
    clearTimeout(closeTimer);
    drawerAbort.abort();
    drawerAbort = new AbortController();
  }

  return {
    drawerEl: drawer,
    scrimEl: scrim,
    isOpen: () => isOpen,
    setOpen,
    renderList,
    syncCardSelection,
    syncSelectionCounters,
    resetPerson,
    destroy,
  };
}
