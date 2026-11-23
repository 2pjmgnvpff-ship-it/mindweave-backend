// chat.js — full-screen Muse chat with Cupid: live agent execution trace, 1-tap action cards,
// proactive care whisper card, voice-first hold-to-speak composer, left-edge iOS swipe-to-pop,
// tactile citations, searchable right-edge drawer, gold-only perspective, crisis safety stop.
import { h, clear, svg, $ } from '../dom.js';
import { api, uuid } from '../api/endpoints.js';
import { friendlyMessage } from '../api/client.js';
import { store } from '../store.js';
import { haptic } from '../native-bridge.js';
import { BACK_SVG, SEND_SVG, PAPERS_SVG, ARROW_SVGS, CUPID_IMG } from '../icons.js';
import { shouldSendOnEnter, matchesMaterialQuery } from '../format.js';
import {
  userBubble, writebackReceipt, aiBubble, actionProposalCard, proactiveBriefCard,
  perspectiveCard, suggestionRow, crisisCard, historyMessage, starterChips, drawerCard, attachedChip,
} from './chat-render.js';
import { createChatVoice, bindChatGestures, clearSwipeStyles } from './chat-voice.js';
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
let voiceCtrl = null;

function syncComposerAction() {
  if (!els.send || !els.mic || !els.input) return;
  const hasText = Boolean(els.input.value.trim());
  const streaming = Boolean(state.streaming);
  const voiceBusy = Boolean(voiceCtrl?.isBusy());
  const transcribing = Boolean(voiceCtrl?.isTranscribing());
  const showMic = (!hasText && !streaming) || voiceBusy;
  els.mic.hidden = !showMic;
  els.mic.disabled = transcribing;
  els.send.hidden = showMic;
  els.send.disabled = !hasText || streaming || transcribing;
  els.input.disabled = transcribing;
}

function build() {
  const root = $('#chat');
  clear(root);
  els.back = h('button', { type: 'button', class: 'muse-glass-circle-btn', 'aria-label': '回到手帐', onClick: () => (haptic('light'), closeChat()) }, svg(BACK_SVG));
  els.drawerBadge = h('span', { class: 'muse-drawer-badge', hidden: true, 'aria-hidden': 'true' });
  els.drawerPill = h('button', { type: 'button', class: 'muse-drawer-pill', 'aria-label': '挑几页手帐给丘比特看', onClick: () => (haptic('light'), toggleDrawer(true)) }, svg(PAPERS_SVG, 'muse-drawer-icon'), h('span', { text: '手帐' }), els.drawerBadge);
  els.medallion = h('div', { class: 'muse-cupid-medallion' }, h('img', { class: 'muse-medallion-img', src: CUPID_IMG, alt: '丘比特' }));
  els.arrow = h('span', { class: 'cupid-inline-arrow', 'aria-hidden': 'true' });
  els.sub = h('span', { class: 'muse-status-sub-row' });
  els.messages = h('div', { class: 'ai-chat-messages', role: 'log', 'aria-label': '和丘比特的对话' });
  els.edge = h('div', { class: 'chat-right-edge-gesture-zone', 'aria-hidden': 'true' });
  els.scrim = h('div', { class: 'chat-material-scrim', onClick: () => toggleDrawer(false) });
  els.drawerList = h('div', { class: 'drawer-materials-list' });
  els.drawerCount = h('span', { class: 'drawer-count-pill', text: '0' });
  els.drawerSearch = h('input', { type: 'search', class: 'drawer-search-input', placeholder: '翻找旧手帐…', 'aria-label': '搜索手帐', autocomplete: 'off' });
  els.drawerSearchClear = h('button', { type: 'button', class: 'drawer-search-clear', 'aria-label': '清空搜索', text: '✕', hidden: true, onClick: () => (haptic('light'), resetDrawerSearch(), renderDrawer(), els.drawerSearch.focus({ preventScroll: true })) });
  els.drawerSearchWrap = h('div', { class: 'drawer-search-wrap', hidden: true }, h('span', { class: 'drawer-search-icon', 'aria-hidden': 'true', text: '⌕' }), els.drawerSearch, els.drawerSearchClear);
  els.drawer = h('aside', { class: 'chat-material-drawer', 'aria-label': '挑几页手帐', 'aria-hidden': 'true' }, h('div', { class: 'drawer-inner-header' }, h('div', {}, h('p', { class: 'drawer-title', text: '挑几页给丘比特看' }), h('p', { class: 'drawer-subtitle', text: '选中的会一起发过去' })), els.drawerCount), els.drawerSearchWrap, els.drawerList);
  els.brief = h('div', { class: 'cupid-brief-host', hidden: true });
  els.starters = h('div', { class: 'cupid-starter-row', role: 'group', 'aria-label': '可以这样问丘比特', hidden: true });
  els.tray = h('div', { class: 'chat-attached-tray', hidden: true });
  els.input = h('textarea', { class: 'chat-text-input', rows: '1', maxlength: '2000', 'aria-label': '想和丘比特说的话', placeholder: '和丘比特说说…' });
  els.send = h('button', { type: 'button', class: 'chat-send-btn', 'aria-label': '发送', hidden: true, disabled: true }, svg(SEND_SVG));
  els.plus = h('button', { type: 'button', class: 'muse-composer-plus-btn', 'aria-label': '附上手帐', text: '+', onClick: () => (haptic('light'), toggleDrawer(true)) });
  els.pillBar = h('div', { class: 'muse-composer-pill-bar' });
  voiceCtrl = createChatVoice({
    pillBar: els.pillBar,
    getPersonId: () => state.personId,
    canRecord: () => !state.streaming && Boolean(state.personId) && !$('#chat').classList.contains('is-crisis'),
    onTranscript: (text) => send(text),
    onStateChange: () => syncComposerAction(),
  });
  els.mic = voiceCtrl.micBtn;
  els.pillBar.append(els.plus, els.input, voiceCtrl.overlayEl, els.send, els.mic);

  const header = h(
    'header',
    { class: 'muse-chat-header' },
    h('div', { class: 'muse-header-controls' }, els.back, els.drawerPill),
    h('div', { class: 'muse-companion-centerpiece' }, h('div', { class: 'muse-medallion-stage' }, h('span', { class: 'muse-cupid-halo', 'aria-hidden': 'true' }), els.medallion), h('div', { class: 'muse-cupid-status-pill' }, h('span', { class: 'muse-status-main-row' }, h('span', { class: 'cupid-chat-name', text: '丘比特' }), els.arrow), els.sub)),
  );
  root.append(header, els.messages, els.edge, els.scrim, els.drawer, h('div', { class: 'ai-chat-composer-wrap' }, els.brief, els.starters, els.tray, els.pillBar));
  els.drawerSearch.addEventListener('input', () => {
    state.drawerQuery = els.drawerSearch.value.trim();
    els.drawerSearchClear.hidden = !state.drawerQuery;
    renderDrawer();
  });
  els.input.addEventListener('input', () => {
    autoGrow();
    if (els.input.value.trim()) hideBrief();
    syncComposerAction();
  });
  els.input.addEventListener('focus', () => setTimeout(scrollDown, 140));
  els.input.addEventListener('keydown', (e) => shouldSendOnEnter(e) && (e.preventDefault(), send()));
  els.send.addEventListener('click', () => send());
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || !root.classList.contains('is-active')) return;
    if (els.drawer.classList.contains('is-open')) toggleDrawer(false);
    else closeChat();
  });
  bindChatGestures({
    root,
    edge: els.edge,
    drawer: els.drawer,
    isVoiceBusy: () => Boolean(voiceCtrl?.isBusy()),
    onOpenDrawer: () => toggleDrawer(true),
    onCloseDrawer: () => toggleDrawer(false),
    onSwipePop: () => closeChat({ fromSwipe: true }),
  });
  syncComposerAction();
  state.built = true;
}

const autoGrow = () => ((els.input.style.height = 'auto'), (els.input.style.height = `${Math.min(120, els.input.scrollHeight)}px`));
const resetDrawerSearch = () => ((state.drawerQuery = ''), els.drawerSearch && (els.drawerSearch.value = ''), els.drawerSearchClear && (els.drawerSearchClear.hidden = true));
const person = () => store.get().people[state.personId] || null;
const scrollDown = () => requestAnimationFrame(() => els.messages && (els.messages.scrollTop = els.messages.scrollHeight));
const hideStarters = () => els.starters && (els.starters.hidden = true);
const hideBrief = () => els.brief && (clear(els.brief), (els.brief.hidden = true));
const stopCrisis = () => $('#chat').classList.remove('is-crisis');

function updateDrawerBadge() {
  const count = state.attached.size;
  if (els.drawerCount) els.drawerCount.textContent = String(count);
  if (els.drawerBadge) ((els.drawerBadge.textContent = String(count)), (els.drawerBadge.hidden = count === 0));
  if (els.drawerPill) els.drawerPill.classList.toggle('has-attached', count > 0);
}

function renderStarters(tier) {
  if (!els.starters) return;
  clear(els.starters);
  els.starters.append(...starterChips(tier, (prompt) => send(prompt)));
  els.starters.hidden = false;
}

function renderBrief(brief, tier) {
  if (!els.brief) return;
  clear(els.brief);
  const card = proactiveBriefCard(brief, tier, (prompt) => send(prompt));
  els.brief.hidden = !card;
  if (card) els.brief.appendChild(card);
}

function isFreshThread(items) {
  if (!items.length) return true;
  if (items.length <= 2 && items[0]?.role === 'user' && String(items[0]?.content || '').trim() === ONBOARDING_QUESTION) {
    return items.length === 1 || items[1]?.role === 'assistant';
  }
  return false;
}

function renderDrawer() {
  const allItems = store.get().materials[state.personId]?.items || [];
  const tier = person()?.tier || 'crystal';
  const showSearch = allItems.length > DRAWER_SEARCH_THRESHOLD;
  els.drawerSearchWrap.hidden = !showSearch;
  if (!showSearch && state.drawerQuery) resetDrawerSearch();
  const items = state.drawerQuery ? allItems.filter((m) => matchesMaterialQuery(m, state.drawerQuery)) : allItems;
  clear(els.drawerList);
  updateDrawerBadge();
  if (!allItems.length) return els.drawerList.appendChild(h('p', { class: 'drawer-empty', text: '这一页还没有手帐。先回去记一句吧。' }));
  if (!items.length) return els.drawerList.appendChild(h('p', { class: 'drawer-empty', text: '没有找到相关的手帐，换个词试试。' }));
  for (const m of items) {
    els.drawerList.appendChild(
      drawerCard(m, {
        selected: state.attached.has(m.id),
        tier,
        onToggle: () => {
          if (state.attached.has(m.id)) state.attached.delete(m.id);
          else if (state.attached.size >= 5) return (haptic('warning'), whisper('一次最多附 5 页', 2000));
          else state.attached.add(m.id);
          haptic('selection');
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

function toggleDrawer(open, { restoreFocus = true } = {}) {
  const wasOpen = els.drawer.classList.contains('is-open');
  if (open) {
    const pid = state.personId;
    if (!store.get().materials[pid]?.loaded) {
      loadMaterials(pid).then(() => state.personId === pid && (renderDrawer(), renderTray()));
    }
    renderDrawer();
  } else {
    resetDrawerSearch();
  }
  els.drawer.classList.toggle('is-open', open);
  els.scrim.classList.toggle('is-open', open);
  els.drawer.setAttribute('aria-hidden', String(!open));
  if (open) (els.drawerSearchWrap.hidden ? els.drawerList.querySelector('button') : els.drawerSearch)?.focus({ preventScroll: true });
  else if (wasOpen && restoreFocus) els.drawerPill?.focus({ preventScroll: true });
}

function frameReply(bubbleEl) {
  requestAnimationFrame(() => {
    const box = els.messages;
    if (!box) return;
    if (!bubbleEl?.isConnected) return (box.scrollTop = box.scrollHeight);
    const relTop = bubbleEl.getBoundingClientRect().top - box.getBoundingClientRect().top + box.scrollTop;
    const maxScroll = Math.max(0, box.scrollHeight - box.clientHeight);
    const span = box.scrollHeight - relTop;
    if (span <= box.clientHeight - 28) box.scrollTop = maxScroll;
    else box.scrollTop = Math.max(0, Math.min(maxScroll, relTop - 24));
  });
}

async function loadHistory({ hasInitialPrompt = false } = {}) {
  const pid = state.personId;
  const seq = ++state.historySeq;
  const p = person();
  clear(els.messages);
  const anchor = document.createComment('history-end');
  els.messages.appendChild(anchor);
  const rawTier = p?.tier || 'crystal';
  const tier = Object.hasOwn(ARROW_SVGS, rawTier) ? rawTier : 'crystal';
  const briefPromise = !hasInitialPrompt && typeof api.brief === 'function' ? api.brief(pid).catch(() => null) : Promise.resolve(null);
  let showFreshCards = false;
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
    showFreshCards = !hasLive && !hasInitialPrompt && isFreshThread(items);
  } catch {
    if (state.personId !== pid || state.historySeq !== seq || !anchor.isConnected) return;
    if (!anchor.nextSibling) {
      els.messages.insertBefore(h('p', { class: 'chat-empty-hint', text: EMPTY_COPY[tier](p.nameCn) }), anchor);
      showFreshCards = !hasInitialPrompt;
    }
    anchor.remove();
  }
  if (showFreshCards) renderStarters(tier);
  else hideStarters();
  scrollDown();
  if (showFreshCards) {
    const brief = await briefPromise;
    if (state.personId !== pid || state.historySeq !== seq || state.streaming || els.input.value.trim() || els.starters.hidden) return;
    if (brief?.headline) renderBrief(brief, tier);
  }
}

async function stream(text, ids, attempt, userEl) {
  const pid = state.personId;
  const rawTier = person()?.tier || 'crystal';
  const tier = Object.hasOwn(ARROW_SVGS, rawTier) ? rawTier : 'crystal';
  const bubble = aiBubble();
  els.messages.appendChild(bubble.el);
  frameReply(bubble.el);
  const ctrl = new AbortController();
  state.streaming = ctrl;
  syncComposerAction();
  let gotError = null;
  let crisis = null;
  let writebackQueued = false;
  const seenProposals = new Set();
  const actionCards = [];
  const extras = [];
  try {
    await api.streamChat(
      pid,
      { message: text, referencedMaterialIds: ids, clientMessageId: attempt.clientMessageId, idem: attempt.idem, signal: ctrl.signal },
      ({ event, data }) => {
        if (event === 'agent_step') bubble.addAgentStep(data);
        else if (event === 'delta') bubble.append(data?.text || '');
        else if (event === 'citation') bubble.addCitation(data, pid);
        else if (event === 'whisper') bubble.setWhisper(data?.text);
        else if (event === 'action_proposal') {
          const propKey = data?.id ? String(data.id) : `${data?.kind}:${data?.title || data?.body || ''}`;
          if (!seenProposals.has(propKey)) {
            seenProposals.add(propKey);
            const card = actionProposalCard(data, pid);
            if (card) actionCards.push(card);
          }
        } else if (event === 'perspective' && tier === 'gold') extras.push(perspectiveCard(data?.items, tier));
        else if (event === 'suggestion') extras.push(suggestionRow(data?.items));
        else if (event === 'crisis') crisis = data;
        else if (event === 'error') gotError = data;
        else if (event === 'done' && data?.writebackQueued === true) writebackQueued = true;
        if (event === 'agent_step' || event === 'delta' || event === 'citation') frameReply(bubble.el);
      },
    );
  } catch (err) {
    if (err?.name === 'AbortError') return bubble.el.remove();
    gotError = { code: err.code, status: err.status, message: err.message };
  } finally {
    if (state.streaming === ctrl) state.streaming = null;
    syncComposerAction();
  }
  if (state.personId !== pid) return;
  if (crisis) {
    bubble.el.replaceWith(crisisCard(crisis, { onContinue: () => stopCrisis() }));
    $('#chat').classList.add('is-crisis');
    return scrollDown();
  }
  if (gotError) {
    bubble.fail(gotError.message || friendlyMessage(gotError.code, gotError.status), () => {
      bubble.el.remove();
      stream(text, ids, attempt, userEl); // same Idempotency-Key + clientMessageId → safe replay
    });
    return scrollDown();
  }
  bubble.finishTrace();
  if (!bubble.started) bubble.setText('丘比特想了想，暂时没有更多可说的。');
  if (writebackQueued) {
    if (userEl?.isConnected && !userEl.nextElementSibling?.classList.contains('chat-writeback-receipt')) userEl.after(writebackReceipt());
    actions.reloadMaterials(pid);
  }
  for (const card of actionCards) els.messages.appendChild(card);
  for (const x of extras) if (x) els.messages.appendChild(x);
  frameReply(bubble.el);
}

function send(overrideText) {
  const fromInput = overrideText === undefined;
  const text = (fromInput ? els.input.value : String(overrideText)).trim();
  if (!text || state.streaming || !state.personId) return;
  haptic('light');
  const attachments = attachedMaterials();
  const ids = attachments.map((m) => m.id);
  hideBrief();
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
  toggleDrawer(false, { restoreFocus: false });
  const attempt = { clientMessageId: uuid(), idem: uuid() };
  state.liveClientIds.add(attempt.clientMessageId);
  stream(text, ids, attempt, userEl);
}

export function openChat(personId, opts = {}) {
  const s = store.get();
  let pid = personId || s.activePersonId;
  if (!pid || !s.people[pid]) pid = s.coreIds[0] || s.casualIds[0] || null;
  if (!pid) return whisper('先记下一个人，再和丘比特聊 TA', 2600);
  if (!state.built) build();
  const root = $('#chat');
  clearSwipeStyles(root);
  const changed = state.personId !== pid;
  if (changed) {
    state.streaming?.abort();
    voiceCtrl?.cancel();
    state.attached.clear();
    resetDrawerSearch();
  }
  state.personId = pid;
  state.liveClientIds.clear();
  const p = s.people[pid];
  const rawTier = p.tier || 'crystal';
  const safeTier = Object.hasOwn(ARROW_SVGS, rawTier) ? rawTier : 'crystal';
  root.dataset.arrowTier = safeTier;
  clear(els.arrow);
  els.arrow.appendChild(svg(ARROW_SVGS[safeTier]));
  els.sub.textContent = SUB_COPY[safeTier](p.nameCn);
  stopCrisis();
  if (opts?.materialId) {
    const mid = String(opts.materialId);
    if (!state.attached.has(mid) && state.attached.size >= 5) state.attached.delete(state.attached.values().next().value);
    state.attached.add(mid);
  }
  const initialPrompt = typeof opts?.initialPrompt === 'string' ? opts.initialPrompt.trim() : '';
  renderTray();
  hideBrief();
  hideStarters();
  syncComposerAction();
  root.hidden = false;
  requestAnimationFrame(() => {
    root.classList.add('is-active');
    els.medallion.classList.remove('is-cupid-greeting');
    void els.medallion.offsetWidth;
    els.medallion.classList.add('is-cupid-greeting');
  });
  for (const id of ['journalCanvas', 'dock']) $(`#${id}`).inert = true;
  loadHistory({ hasInitialPrompt: Boolean(initialPrompt) });
  if (!s.materials[pid]?.loaded) {
    loadMaterials(pid).then(() => state.personId === pid && (renderTray(), els.drawer.classList.contains('is-open') && renderDrawer()));
  }
  if (initialPrompt) send(initialPrompt);
  else setTimeout(() => els.input.focus({ preventScroll: true }), 260);
}

export function closeChat({ fromSwipe = false } = {}) {
  const root = $('#chat');
  if (root.hidden) return;
  state.streaming?.abort();
  voiceCtrl?.cancel();
  state.historySeq += 1;
  toggleDrawer(false, { restoreFocus: false });
  root.classList.remove('is-active');
  for (const id of ['journalCanvas', 'dock']) $(`#${id}`).inert = false;
  setTimeout(() => {
    if (!root.classList.contains('is-active')) {
      clearSwipeStyles(root);
      root.hidden = true;
    }
  }, fromSwipe ? 260 : 340);
  $('#aiOrb')?.focus({ preventScroll: true });
}

/** Ask Cupid a first question programmatically (used by onboarding & proactive CTAs). */
export function sendToCupid(personId, text) {
  openChat(personId, { initialPrompt: text });
}
