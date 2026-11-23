// chat.js — full-screen Muse chat with Cupid about one person. POST + SSE streaming,
// citation tokens that jump back to the page, searchable right-edge drawer of real materials,
// tier-aware starter prompts, writeback receipt, IME-safe composer, crisis card safety stop.
import { h, clear, svg, $ } from '../dom.js';
import { api, uuid } from '../api/endpoints.js';
import { friendlyMessage } from '../api/client.js';
import { store } from '../store.js';
import { BACK_SVG, SEND_SVG, PAPERS_SVG, ARROW_SVGS, CUPID_IMG } from '../icons.js';
import { shouldSendOnEnter, formatDateCn } from '../format.js';
import {
  userBubble,
  writebackReceipt,
  aiBubble,
  perspectiveCard,
  suggestionRow,
  crisisCard,
  historyMessage,
  starterChips,
  drawerCard,
  attachedChip,
} from './chat-render.js';
import { loadMaterials } from './journal.js';
import { actions } from '../actions.js';
import { whisper } from './toast.js';

const SUB_COPY = {
  gold: (n) => `陪你多替「${n}」想一步`,
  silver: (n) => `站在你这边，聊聊「${n}」`,
  crystal: (n) => `帮你把「${n}」的礼数记周全`,
};
const EMPTY_COPY = {
  gold: (n) => `想聊聊「${n}」的什么？丘比特会翻着你记下的手帐回答，每句都标出处。`,
  silver: (n) => `和「${n}」之间不痛快？说吧，丘比特站你这边，只是不会替你编事实。`,
  crystal: (n) => `要给「${n}」回个消息、备份礼物？问丘比特就好。`,
};
const ONBOARDING_QUESTION = '你记住了什么？';
const DRAWER_SEARCH_THRESHOLD = 6;

let state = { personId: null, attached: new Set(), streaming: null, built: false, historySeq: 0, liveClientIds: new Set(), drawerQuery: '' };
let els = {};

function build() {
  const root = $('#chat');
  clear(root);
  els.back = h('button', { type: 'button', class: 'muse-glass-circle-btn', 'aria-label': '回到手帐', onClick: () => closeChat() }, svg(BACK_SVG));
  els.drawerBadge = h('span', { class: 'muse-drawer-badge', hidden: true, 'aria-hidden': 'true' });
  els.drawerPill = h(
    'button',
    { type: 'button', class: 'muse-drawer-pill', 'aria-label': '挑几页手帐给丘比特看', onClick: () => toggleDrawer(true) },
    svg(PAPERS_SVG, 'muse-drawer-icon'),
    h('span', { text: '手帐' }),
    els.drawerBadge,
  );
  els.medallion = h('div', { class: 'muse-cupid-medallion' }, h('img', { class: 'muse-medallion-img', src: CUPID_IMG, alt: '丘比特' }));
  els.arrow = h('span', { class: 'cupid-inline-arrow', 'aria-hidden': 'true' });
  els.sub = h('span', { class: 'muse-status-sub-row' });
  els.messages = h('div', { class: 'ai-chat-messages', role: 'log', 'aria-label': '和丘比特的对话' });
  els.edge = h('div', { class: 'chat-right-edge-gesture-zone', 'aria-hidden': 'true' });
  els.scrim = h('div', { class: 'chat-material-scrim', onClick: () => toggleDrawer(false) });
  els.drawerList = h('div', { class: 'drawer-materials-list' });
  els.drawerCount = h('span', { class: 'drawer-count-pill', text: '0' });
  els.drawerSearch = h('input', { type: 'search', class: 'drawer-search-input', placeholder: '翻找旧手帐…', 'aria-label': '搜索手帐', autocomplete: 'off' });
  els.drawerSearchClear = h('button', {
    type: 'button',
    class: 'drawer-search-clear',
    'aria-label': '清空搜索',
    text: '✕',
    hidden: true,
    onClick: () => {
      resetDrawerSearch();
      renderDrawer();
      els.drawerSearch.focus({ preventScroll: true });
    },
  });
  els.drawerSearchWrap = h('div', { class: 'drawer-search-wrap', hidden: true }, h('span', { class: 'drawer-search-icon', 'aria-hidden': 'true', text: '⌕' }), els.drawerSearch, els.drawerSearchClear);
  els.drawer = h(
    'aside',
    { class: 'chat-material-drawer', 'aria-label': '挑几页手帐', 'aria-hidden': 'true' },
    h('div', { class: 'drawer-inner-header' }, h('div', {}, h('p', { class: 'drawer-title', text: '挑几页给丘比特看' }), h('p', { class: 'drawer-subtitle', text: '选中的会一起发过去' })), els.drawerCount),
    els.drawerSearchWrap,
    els.drawerList,
  );
  els.starters = h('div', { class: 'cupid-starter-row', role: 'group', 'aria-label': '可以这样问丘比特', hidden: true });
  els.tray = h('div', { class: 'chat-attached-tray', hidden: true });
  els.input = h('textarea', { class: 'chat-text-input', rows: '1', maxlength: '2000', 'aria-label': '想和丘比特说的话', placeholder: '和丘比特说说…' });
  els.send = h('button', { type: 'button', class: 'chat-send-btn', 'aria-label': '发送', disabled: true }, svg(SEND_SVG));
  els.plus = h('button', { type: 'button', class: 'muse-composer-plus-btn', 'aria-label': '附上手帐', text: '+', onClick: () => toggleDrawer(true) });
  root.append(
    h(
      'header',
      { class: 'muse-chat-header' },
      h('div', { class: 'muse-header-controls' }, els.back, els.drawerPill),
      h(
        'div',
        { class: 'muse-companion-centerpiece' },
        h('div', { class: 'muse-medallion-stage' }, h('span', { class: 'muse-cupid-halo', 'aria-hidden': 'true' }), els.medallion),
        h('div', { class: 'muse-cupid-status-pill' }, h('span', { class: 'muse-status-main-row' }, h('span', { class: 'cupid-chat-name', text: '丘比特' }), els.arrow), els.sub),
      ),
    ),
    els.messages,
    els.edge,
    els.scrim,
    els.drawer,
    h('div', { class: 'ai-chat-composer-wrap' }, els.starters, els.tray, h('div', { class: 'muse-composer-pill-bar' }, els.plus, els.input, els.send)),
  );
  els.drawerSearch.addEventListener('input', () => {
    state.drawerQuery = els.drawerSearch.value.trim();
    els.drawerSearchClear.hidden = !state.drawerQuery;
    renderDrawer();
  });
  els.input.addEventListener('input', () => {
    autoGrow();
    els.send.disabled = !els.input.value.trim() || Boolean(state.streaming);
  });
  els.input.addEventListener('keydown', (e) => {
    if (shouldSendOnEnter(e)) {
      e.preventDefault();
      send();
    }
  });
  els.send.addEventListener('click', () => send());
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || !root.classList.contains('is-active')) return;
    if (els.drawer.classList.contains('is-open')) toggleDrawer(false);
    else closeChat();
  });
  edgeSwipe();
  state.built = true;
}

function autoGrow() {
  els.input.style.height = 'auto';
  els.input.style.height = `${Math.min(120, els.input.scrollHeight)}px`;
}

function resetDrawerSearch() {
  state.drawerQuery = '';
  if (els.drawerSearch) els.drawerSearch.value = '';
  if (els.drawerSearchClear) els.drawerSearchClear.hidden = true;
}

function edgeSwipe() {
  let start = null;
  els.edge.addEventListener('pointerdown', (e) => {
    start = { x: e.clientX, id: e.pointerId };
    els.edge.setPointerCapture?.(e.pointerId);
  });
  els.edge.addEventListener('pointermove', (e) => {
    if (start && start.id === e.pointerId && start.x - e.clientX > 28) {
      start = null;
      toggleDrawer(true);
    }
  });
  els.edge.addEventListener('pointerup', () => (start = null));
  let dstart = null;
  els.drawer.addEventListener('pointerdown', (e) => (dstart = { x: e.clientX, y: e.clientY }));
  els.drawer.addEventListener('pointerup', (e) => {
    if (dstart && e.clientX - dstart.x > 60 && Math.abs(e.clientY - dstart.y) < 50) toggleDrawer(false);
    dstart = null;
  });
}

function person() {
  return store.get().people[state.personId] || null;
}

function scrollDown() {
  requestAnimationFrame(() => {
    if (els.messages) els.messages.scrollTop = els.messages.scrollHeight;
  });
}

function updateDrawerBadge() {
  const count = state.attached.size;
  if (els.drawerCount) els.drawerCount.textContent = String(count);
  if (els.drawerBadge) {
    els.drawerBadge.textContent = String(count);
    els.drawerBadge.hidden = count === 0;
  }
  if (els.drawerPill) els.drawerPill.classList.toggle('has-attached', count > 0);
}

function renderStarters(tier) {
  if (!els.starters) return;
  clear(els.starters);
  els.starters.append(...starterChips(tier, (prompt) => send(prompt)));
  els.starters.hidden = false;
}

function hideStarters() {
  if (els.starters) els.starters.hidden = true;
}

function isFreshThread(items) {
  if (!items.length) return true;
  if (items.length <= 2 && items[0]?.role === 'user' && String(items[0]?.content || '').trim() === ONBOARDING_QUESTION) {
    return items.length === 1 || items[1]?.role === 'assistant';
  }
  return false;
}

function matchesDrawerQuery(m, q) {
  if (!q) return true;
  return [m.snippet, m.body, formatDateCn(m.happenedOn || m.createdAt), m.happenedOn].filter(Boolean).join(' ').toLowerCase().includes(q);
}

function renderDrawer() {
  const allItems = store.get().materials[state.personId]?.items || [];
  const tier = person()?.tier || 'crystal';
  const showSearch = allItems.length > DRAWER_SEARCH_THRESHOLD;
  els.drawerSearchWrap.hidden = !showSearch;
  if (!showSearch && state.drawerQuery) resetDrawerSearch();
  const q = state.drawerQuery.toLowerCase();
  const items = q ? allItems.filter((m) => matchesDrawerQuery(m, q)) : allItems;
  clear(els.drawerList);
  updateDrawerBadge();
  if (!allItems.length) {
    els.drawerList.appendChild(h('p', { class: 'drawer-empty', text: '这一页还没有手帐。先回去记一句吧。' }));
    return;
  }
  if (!items.length) {
    els.drawerList.appendChild(h('p', { class: 'drawer-empty', text: '没有找到相关的手帐，换个词试试。' }));
    return;
  }
  for (const m of items) {
    els.drawerList.appendChild(
      drawerCard(m, {
        selected: state.attached.has(m.id),
        tier,
        onToggle: () => {
          if (state.attached.has(m.id)) state.attached.delete(m.id);
          else if (state.attached.size >= 5) return whisper('一次最多附 5 页', 2000);
          else state.attached.add(m.id);
          renderDrawer();
          renderTray();
        },
      }),
    );
  }
}

function attachedMaterials() {
  const byId = new Map((store.get().materials[state.personId]?.items || []).map((m) => [m.id, m]));
  return [...state.attached].map((id) => byId.get(id) || { id, type: 'note', snippet: '已选一页手帐' });
}

function renderTray() {
  clear(els.tray);
  const list = attachedMaterials();
  els.tray.hidden = !list.length;
  updateDrawerBadge();
  for (const m of list) {
    els.tray.appendChild(
      attachedChip(m, () => {
        state.attached.delete(m.id);
        renderTray();
        if (els.drawer.classList.contains('is-open')) renderDrawer();
      }),
    );
  }
}

function toggleDrawer(open) {
  if (open) {
    const pid = state.personId;
    if (!store.get().materials[pid]?.loaded) {
      loadMaterials(pid).then(() => {
        if (state.personId === pid) {
          renderDrawer();
          renderTray();
        }
      });
    }
    renderDrawer();
  } else {
    resetDrawerSearch();
  }
  els.drawer.classList.toggle('is-open', open);
  els.scrim.classList.toggle('is-open', open);
  els.drawer.setAttribute('aria-hidden', String(!open));
  if (open) (els.drawerSearchWrap.hidden ? els.drawerList.querySelector('button') : els.drawerSearch)?.focus({ preventScroll: true });
}

async function loadHistory() {
  const pid = state.personId;
  const seq = ++state.historySeq;
  const p = person();
  clear(els.messages);
  const anchor = document.createComment('history-end');
  els.messages.appendChild(anchor);
  const tier = p?.tier || 'crystal';
  try {
    const res = await api.chatHistory(pid, { limit: 40 });
    if (state.personId !== pid || state.historySeq !== seq || !anchor.isConnected) return;
    const rawItems = res.items || [];
    const liveIdx = rawItems.findIndex((m) => m.clientMessageId && state.liveClientIds.has(m.clientMessageId));
    const items = liveIdx >= 0 ? rawItems.slice(0, liveIdx) : rawItems;
    const hasLive = Boolean(anchor.nextSibling);
    if (!items.length && !hasLive) {
      els.messages.insertBefore(h('p', { class: 'chat-empty-hint', text: EMPTY_COPY[tier](p.nameCn) }), anchor);
    } else if (items.length) {
      const frag = document.createDocumentFragment();
      for (const m of items) frag.append(...historyMessage(m, pid, tier));
      els.messages.insertBefore(frag, anchor);
    }
    anchor.remove();
    if (!hasLive && isFreshThread(items)) renderStarters(tier);
    else hideStarters();
  } catch {
    if (state.personId !== pid || state.historySeq !== seq || !anchor.isConnected) return;
    const hasLive = Boolean(anchor.nextSibling);
    if (!hasLive) {
      els.messages.insertBefore(h('p', { class: 'chat-empty-hint', text: EMPTY_COPY[tier](p.nameCn) }), anchor);
      renderStarters(tier);
    }
    anchor.remove();
  }
  scrollDown();
}

function stopCrisis() {
  $('#chat').classList.remove('is-crisis');
}

async function stream(text, ids, attempt, userEl) {
  const pid = state.personId;
  const p = person();
  const tier = p?.tier || 'crystal';
  const bubble = aiBubble();
  els.messages.appendChild(bubble.el);
  scrollDown();
  const ctrl = new AbortController();
  state.streaming = ctrl;
  els.send.disabled = true;
  let gotError = null;
  let crisis = null;
  let writebackQueued = false;
  const extras = [];
  try {
    await api.streamChat(
      pid,
      { message: text, referencedMaterialIds: ids, clientMessageId: attempt.clientMessageId, idem: attempt.idem, signal: ctrl.signal },
      ({ event, data }) => {
        if (event === 'delta') bubble.append(data?.text || '');
        else if (event === 'citation') bubble.addCitation(data, pid);
        else if (event === 'whisper') bubble.setWhisper(data?.text);
        else if (event === 'perspective' && tier === 'gold') extras.push(perspectiveCard(data?.items, tier));
        else if (event === 'suggestion') extras.push(suggestionRow(data?.items));
        else if (event === 'crisis') crisis = data;
        else if (event === 'error') gotError = data;
        else if (event === 'done' && data?.writebackQueued === true) writebackQueued = true;
        if (event === 'delta' || event === 'citation') scrollDown();
      },
    );
  } catch (err) {
    if (err?.name === 'AbortError') {
      bubble.el.remove();
      return;
    }
    gotError = { code: err.code, status: err.status, message: err.message };
  } finally {
    if (state.streaming === ctrl) state.streaming = null;
    els.send.disabled = !els.input.value.trim();
  }
  if (state.personId !== pid) return;
  if (crisis) {
    bubble.el.replaceWith(crisisCard(crisis, { onContinue: () => stopCrisis() }));
    $('#chat').classList.add('is-crisis');
    scrollDown();
    return;
  }
  if (gotError) {
    bubble.fail(gotError.message || friendlyMessage(gotError.code, gotError.status), () => {
      bubble.el.remove();
      stream(text, ids, attempt, userEl); // same Idempotency-Key + clientMessageId → safe replay
    });
    scrollDown();
    return;
  }
  if (!bubble.started) bubble.setText('丘比特想了想，暂时没有更多可说的。');
  if (writebackQueued) {
    if (userEl?.isConnected && !userEl.nextElementSibling?.classList.contains('chat-writeback-receipt')) {
      userEl.after(writebackReceipt());
    }
    actions.reloadMaterials(pid);
  }
  for (const x of extras) if (x) els.messages.appendChild(x);
  scrollDown();
}

function send(overrideText) {
  const fromInput = overrideText === undefined;
  const text = (fromInput ? els.input.value : String(overrideText)).trim();
  if (!text || state.streaming || !state.personId) return;
  const attachments = attachedMaterials();
  const ids = attachments.map((m) => m.id);
  hideStarters();
  els.messages.querySelector('.chat-empty-hint')?.remove();
  const userEl = userBubble(text, attachments);
  els.messages.appendChild(userEl);
  if (fromInput) {
    els.input.value = '';
    autoGrow();
  }
  state.attached.clear();
  renderTray();
  toggleDrawer(false);
  const attempt = { clientMessageId: uuid(), idem: uuid() };
  state.liveClientIds.add(attempt.clientMessageId);
  stream(text, ids, attempt, userEl);
}

export function openChat(personId, opts = {}) {
  const s = store.get();
  let pid = personId || s.activePersonId;
  if (!pid || !s.people[pid]) pid = s.coreIds[0] || s.casualIds[0] || null;
  if (!pid) {
    whisper('先记下一个人，再和丘比特聊 TA', 2600);
    return;
  }
  if (!state.built) build();
  const root = $('#chat');
  const changed = state.personId !== pid;
  if (changed) state.streaming?.abort();
  state.personId = pid;
  state.liveClientIds.clear();
  const p = s.people[pid];
  const tier = p.tier || 'crystal';
  root.dataset.arrowTier = tier;
  clear(els.arrow);
  els.arrow.appendChild(svg(ARROW_SVGS[tier]));
  els.sub.textContent = SUB_COPY[tier](p.nameCn);
  stopCrisis();
  if (changed) {
    state.attached.clear();
    resetDrawerSearch();
  }
  if (opts?.materialId) {
    const mid = String(opts.materialId);
    if (!state.attached.has(mid) && state.attached.size >= 5) {
      const oldest = state.attached.values().next().value;
      if (oldest !== undefined) state.attached.delete(oldest);
    }
    state.attached.add(mid);
  }
  renderTray();
  hideStarters();
  root.hidden = false;
  requestAnimationFrame(() => {
    root.classList.add('is-active');
    els.medallion.classList.remove('is-cupid-greeting');
    void els.medallion.offsetWidth;
    els.medallion.classList.add('is-cupid-greeting');
  });
  for (const id of ['journalCanvas', 'dock']) $(`#${id}`).inert = true;
  loadHistory();
  if (!s.materials[pid]?.loaded) {
    loadMaterials(pid).then(() => {
      if (state.personId !== pid) return;
      renderTray();
      if (els.drawer.classList.contains('is-open')) renderDrawer();
    });
  }
  setTimeout(() => els.input.focus({ preventScroll: true }), 260);
}

export function closeChat() {
  const root = $('#chat');
  if (root.hidden) return;
  state.streaming?.abort();
  state.historySeq += 1;
  toggleDrawer(false);
  root.classList.remove('is-active');
  for (const id of ['journalCanvas', 'dock']) $(`#${id}`).inert = false;
  setTimeout(() => {
    if (!root.classList.contains('is-active')) root.hidden = true;
  }, 340);
  $('#aiOrb')?.focus({ preventScroll: true });
}

/** Ask Cupid a first question programmatically (used by onboarding). */
export function sendToCupid(personId, text) {
  openChat(personId);
  els.input.value = text;
  send();
}
