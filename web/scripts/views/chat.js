// chat.js — Gleb Porcelain Cupid Chat (A1 24px Orb header, B4 Dual-Voice bar, BC3 order=desc pagination,
// SSE truncation retry, 1:1 right-edge Memory Drawer, scrollChatToTop, LK-4 display:none teardown).
import { h, clear, svg, $, ensureStyle } from '../dom.js';
import { api, uuid } from '../api/endpoints.js';
import { friendlyMessage } from '../api/client.js';
import { store } from '../store.js';
import { haptic } from '../native-bridge.js';
import { BACK_SVG, SEND_SVG, PAPERS_SVG, ARROW_SVGS } from '../icons.js';
import { createOrb, setOrbState } from '../lib/orb.js';
import { createImeGuard } from '../lib/ime-guard.js';
import { initKeyboardViewportBridge } from '../lib/swipe-arbiter.js';
import {
  userBubble, writebackReceipt, aiBubble, actionProposalCard,
  perspectiveCard, suggestionRow, crisisCard, historyMessage, starterChips, attachedChip,
} from './chat-render.js';
import { createChatVoice, bindChatEdgePop, clearSwipeStyles } from './chat-voice.js';
import { createChatDrawer } from './chat-drawer.js';
import { loadMaterials } from './journal.js';
import { actions } from '../actions.js';
import { whisper } from './toast.js';

const B4_TIP_KEY = 'mw.b4TipSeen';
const SUB_COPY = { gold: (n) => `陪你多替「${n}」想一步`, silver: (n) => `客观整理，聊聊「${n}」`, crystal: (n) => `帮你把「${n}」的细节记周全` };
const EMPTY_COPY = {
  gold: (n) => `想聊聊「${n}」的什么？丘比特会翻着你记下的手帐回答，每句都标出处`,
  silver: (n) => `需要理清和「${n}」的边界或时间线？丘比特只据实整理，不替你编造事实`,
  crystal: (n) => `要给「${n}」回个消息或记件小事？直接问丘比特就好`,
};
const STOP_SVG = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="2.5"/></svg>';

const state = { personId: null, attached: new Set(), streaming: null, built: false, historySeq: 0, nextCursor: null, loadingOlder: false, liveClientIds: new Set() };
const els = {};
let voiceCtrl = null;
let drawerCtrl = null;
let chatAbort = null;
let closeTimer = null;

const isTipSeen = () => { try { return localStorage.getItem(B4_TIP_KEY) === '1'; } catch { return true; } };
const dismissTip = () => {
  if (!els.tipKit || els.tipKit.hidden) return;
  els.tipKit.hidden = true;
  try { localStorage.setItem(B4_TIP_KEY, '1'); } catch { /* ignore */ }
};
const autoGrow = () => ((els.input.style.height = 'auto'), (els.input.style.height = `${Math.min(120, els.input.scrollHeight)}px`));
const person = () => store.get().people[state.personId] || null;
const scrollDown = () => requestAnimationFrame(() => els.messages && (els.messages.scrollTop = els.messages.scrollHeight));
const hideStarters = () => els.starters && ((els.starters.hidden = true), (els.starters.style.display = 'none'), clear(els.starters));
const stopCrisis = () => $('#chat')?.classList.remove('is-crisis');

export function scrollChatToTop(smooth = true) {
  els.messages?.scrollTo({ top: 0, behavior: smooth ? 'smooth' : 'auto' });
}

function syncComposerAction() {
  if (!els.orbBtn || !els.input) return;
  const hasText = Boolean(els.input.value.trim());
  const mode = state.streaming ? 'stop' : hasText && !voiceCtrl?.isBusy() ? 'send' : 'orb';
  els.orbBtn.dataset.mode = mode;
  els.orbBtn.classList.toggle('is-send', mode === 'send');
  els.orbBtn.classList.toggle('is-stop', mode === 'stop');
  els.orbBtn.setAttribute('aria-label', mode === 'stop' ? '停止回复' : mode === 'send' ? '发送' : '轻点直接语音畅聊，按住松开发送');
  els.sendIcon.hidden = mode !== 'send';
  els.sendIcon.style.display = mode === 'send' ? '' : 'none';
  els.stopIcon.hidden = mode !== 'stop';
  els.stopIcon.style.display = mode === 'stop' ? '' : 'none';
  els.dockOrb.hidden = mode !== 'orb';
  els.dockOrb.style.display = mode === 'orb' ? '' : 'none';
  els.input.disabled = Boolean(voiceCtrl?.isTranscribing());
}

function build() {
  const root = $('#chat');
  clear(root);
  chatAbort?.abort();
  chatAbort = new AbortController();
  const { signal } = chatAbort;

  els.back = h('button', { type: 'button', class: 'muse-glass-circle-btn', 'aria-label': '回到手帐', onClick: () => (haptic('light'), closeChat()) }, svg(BACK_SVG));
  els.headerOrb = createOrb({ size: 'header', state: 'idle' });
  els.titleName = h('span', { class: 'cupid-chat-name', text: '丘比特' });
  els.arrow = h('span', { class: 'cupid-inline-arrow', 'aria-hidden': 'true' });
  els.drawerBadge = h('span', { class: 'muse-drawer-badge', hidden: true, 'aria-hidden': 'true' });
  els.drawerPill = h('button', { type: 'button', class: 'muse-drawer-pill', 'aria-label': '挑几页手帐', onClick: () => (haptic('light'), drawerCtrl?.setOpen(true)) }, svg(PAPERS_SVG, 'muse-drawer-icon'), h('span', { text: '手帐' }), els.drawerBadge);
  els.newThreadBtn = h('button', { type: 'button', class: 'muse-new-thread-btn', text: '新对话', onClick: () => startNewThread() });

  els.inner = h('div', { class: 'ai-chat-inner' });
  els.messages = h('div', { class: 'ai-chat-messages', role: 'log', 'aria-label': '和丘比特的对话' }, els.inner);
  els.starters = h('div', { class: 'cupid-starter-row', role: 'group', 'aria-label': '可以这样问丘比特', hidden: true, style: { display: 'none' } });
  els.tray = h('div', { class: 'chat-attached-tray', hidden: true });
  els.tipKit = h('button', { type: 'button', class: 'b4-tipkit-popover', text: '轻点右侧灵珠直接语音畅聊，框内话筒仅转文字', hidden: isTipSeen(), onClick: () => (haptic('light'), dismissTip()) });

  els.plus = h('button', { type: 'button', class: 'muse-composer-plus-btn', 'aria-label': '附上手帐', text: '+', onClick: () => (haptic('light'), drawerCtrl?.setOpen(true)) });
  els.input = h('textarea', { class: 'chat-text-input', rows: '1', maxlength: '2000', 'aria-label': '想和丘比特说的话', placeholder: '和丘比特说说…' });
  els.dockOrb = createOrb({ size: 'dock', state: 'idle' });
  els.sendIcon = svg(SEND_SVG, 'chat-send-icon');
  els.stopIcon = svg(STOP_SVG, 'chat-stop-icon');
  if (els.sendIcon?.style) { els.sendIcon.style.width = '18px'; els.sendIcon.style.height = '18px'; }
  if (els.stopIcon?.style) { els.stopIcon.style.width = '18px'; els.stopIcon.style.height = '18px'; }
  els.sendIcon.hidden = true;
  els.stopIcon.hidden = true;
  els.orbBtn = h('button', { type: 'button', id: 'chatOrbBtn', class: 'chat-detached-orb-btn', dataset: { mode: 'orb' } }, els.dockOrb, els.sendIcon, els.stopIcon);
  els.pillBar = h('div', { class: 'muse-composer-pill-bar' });

  voiceCtrl = createChatVoice({
    pillBar: els.pillBar, orbBtn: els.orbBtn, orbEl: els.dockOrb, inputEl: els.input,
    getPersonId: () => state.personId,
    canRecord: () => !state.streaming && Boolean(state.personId) && !root.classList.contains('is-crisis'),
    onDirectSend: (text) => (dismissTip(), send(text)),
    onDictateAppend: (text, baseText, { preview = false } = {}) => {
      dismissTip();
      const base = String(baseText || '').trim();
      els.input.value = base ? `${base} ${text}` : text;
      autoGrow();
      if (!preview) syncComposerAction();
    },
    onStateChange: () => syncComposerAction(),
  });
  els.pillBar.append(els.plus, els.input, voiceCtrl.statusChip, voiceCtrl.inlineMicBtn, voiceCtrl.dictateDoneBtn);

  drawerCtrl = createChatDrawer({
    root, getPerson: () => person(), getAttached: () => state.attached,
    onToggleAttach: (id) => toggleAttach(id),
    onApplySelection: () => els.input.focus({ preventScroll: true }),
    onOpenPeek: (mid) => actions.openPeek?.(mid, person(), { fromChat: true }),
  });

  const header = h('header', { class: 'ai-chat-header muse-chat-header' },
    h('div', { class: 'ai-chat-header-left' }, els.back, h('div', { class: 'ai-chat-header-title' }, els.headerOrb, els.titleName, els.arrow)),
    h('div', { class: 'ai-chat-header-actions' }, els.drawerPill, els.newThreadBtn),
  );
  root.append(header, els.messages, drawerCtrl.scrimEl, drawerCtrl.drawerEl, h('div', { class: 'muse-composer-wrap ai-chat-composer-wrap' }, els.starters, els.tray, els.tipKit, h('div', { class: 'muse-composer-row' }, els.pillBar, els.orbBtn)));

  createImeGuard(els.input, { isTouchDevice: Boolean(globalThis.matchMedia?.('(pointer: coarse)')?.matches), onSubmit: () => send() });
  els.input.addEventListener('input', () => (autoGrow(), saveDraft(), syncComposerAction()), { signal });
  els.input.addEventListener('focus', () => setTimeout(scrollDown, 120), { signal });
  els.orbBtn.addEventListener('click', () => {
    if (els.orbBtn.dataset.mode === 'stop') { haptic('light'); state.streaming?.abort(); }
    else if (els.orbBtn.dataset.mode === 'send') send();
  }, { signal });
  els.messages.addEventListener('scroll', () => els.messages.scrollTop < 48 && state.nextCursor && !state.loadingOlder && loadOlderHistory(), { passive: true, signal });
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || !root.classList.contains('is-active')) return;
    if (drawerCtrl?.isOpen()) drawerCtrl.setOpen(false);
    else closeChat();
  }, { signal });

  bindChatEdgePop({ root, isDrawerOpen: () => Boolean(drawerCtrl?.isOpen()), isVoiceBusy: () => Boolean(voiceCtrl?.isBusy()), onSwipePop: () => closeChat({ fromSwipe: true }), signal });
  initKeyboardViewportBridge?.({ signal });
  syncComposerAction();
  state.built = true;
}

function saveDraft() {
  if (!state.personId || !els.input) return;
  const drafts = { ...(store.get().chatDraftsByPerson || {}) };
  if (els.input.value) drafts[state.personId] = els.input.value;
  else delete drafts[state.personId];
  store.set({ chatDraftsByPerson: drafts });
}

function updateDrawerBadge() {
  const count = state.attached.size;
  if (els.drawerBadge) ((els.drawerBadge.textContent = String(count)), (els.drawerBadge.hidden = count === 0));
  if (els.drawerPill) els.drawerPill.classList.toggle('has-attached', count > 0);
  drawerCtrl?.syncSelectionCounters();
}

function toggleAttach(materialId) {
  let selected;
  if (state.attached.has(materialId)) { state.attached.delete(materialId); selected = false; }
  else if (state.attached.size >= 5) { haptic('warning'); whisper('一次最多附 5 页', 2000); return null; }
  else { state.attached.add(materialId); selected = true; }
  haptic('selection');
  renderTray();
  return selected;
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
    els.tray.appendChild(attachedChip(m, () => { state.attached.delete(m.id); renderTray(); drawerCtrl?.syncCardSelection(m.id, false); }));
  }
}

function renderStarters(tier, nameCn) {
  if (!els.starters) return;
  clear(els.starters);
  els.starters.append(...starterChips(tier, (prompt) => send(prompt), nameCn));
  els.starters.hidden = false;
  els.starters.style.display = '';
}

function emptyNotice(tier, nameCn) {
  return h('div', { class: 'ai-chat-empty' },
    h('p', { class: 'ai-chat-empty-title', text: SUB_COPY[tier](nameCn || '') }),
    h('p', { class: 'ai-chat-empty-sub', text: EMPTY_COPY[tier](nameCn || '') }),
  );
}

function startNewThread() {
  if (!state.personId) return;
  haptic('light');
  state.streaming?.abort();
  state.nextCursor = null;
  clear(els.inner);
  const p = person();
  const tier = Object.hasOwn(ARROW_SVGS, p?.tier) ? p.tier : 'crystal';
  els.inner.appendChild(emptyNotice(tier, p?.nameCn));
  renderStarters(tier, p?.nameCn);
  els.input?.focus({ preventScroll: true });
}

async function loadHistory({ hasInitialPrompt = false } = {}) {
  const pid = state.personId;
  const seq = ++state.historySeq;
  const p = person();
  const tier = Object.hasOwn(ARROW_SVGS, p?.tier) ? p.tier : 'crystal';
  state.nextCursor = null;
  state.loadingOlder = false;
  clear(els.inner);
  const anchor = document.createComment('history-end');
  els.inner.appendChild(anchor);
  try {
    const res = await api.chatHistory(pid, { order: 'desc', limit: 40 });
    if (state.personId !== pid || state.historySeq !== seq || !anchor.isConnected) return;
    state.nextCursor = res?.nextCursor || null;
    const items = [...(res?.items || [])].reverse().filter((m) => !(m.clientMessageId && state.liveClientIds.has(m.clientMessageId)));
    const hasLive = Boolean(anchor.nextSibling);
    if (!items.length && !hasLive) {
      els.inner.insertBefore(emptyNotice(tier, p?.nameCn), anchor);
      if (!hasInitialPrompt) renderStarters(tier, p?.nameCn);
    } else if (items.length) {
      const frag = document.createDocumentFragment();
      for (const m of items) frag.append(...historyMessage(m, pid, tier));
      els.inner.insertBefore(frag, anchor);
      hideStarters();
    }
    anchor.remove();
  } catch {
    if (state.personId !== pid || state.historySeq !== seq || !anchor.isConnected) return;
    if (!anchor.nextSibling) {
      els.inner.insertBefore(emptyNotice(tier, p?.nameCn), anchor);
      if (!hasInitialPrompt) renderStarters(tier, p?.nameCn);
    }
    anchor.remove();
  }
  scrollDown();
}

async function loadOlderHistory() {
  const pid = state.personId;
  const cursor = state.nextCursor;
  if (!pid || !cursor || state.loadingOlder) return;
  state.loadingOlder = true;
  const seq = state.historySeq;
  const tier = Object.hasOwn(ARROW_SVGS, person()?.tier) ? person().tier : 'crystal';
  try {
    const res = await api.chatHistory(pid, { order: 'desc', limit: 40, cursor });
    if (state.personId !== pid || state.historySeq !== seq) return;
    state.nextCursor = res?.nextCursor || null;
    const older = [...(res?.items || [])].reverse();
    if (older.length) {
      const prevHeight = els.messages.scrollHeight;
      const frag = document.createDocumentFragment();
      for (const m of older) frag.append(...historyMessage(m, pid, tier));
      els.inner.prepend(frag);
      els.messages.scrollTop += els.messages.scrollHeight - prevHeight;
    }
  } catch { /* keep scroll */ } finally { state.loadingOlder = false; }
}

async function stream(text, ids, attempt, userEl) {
  const pid = state.personId;
  const tier = Object.hasOwn(ARROW_SVGS, person()?.tier) ? person().tier : 'crystal';
  const bubble = aiBubble();
  els.inner.appendChild(bubble.el);
  scrollDown();
  const ctrl = new AbortController();
  state.streaming = ctrl;
  setOrbState(els.headerOrb, 'thinking');
  syncComposerAction();

  let gotError = null;
  let crisis = null;
  let receivedDone = false;
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
        else if (event === 'delta') { setOrbState(els.headerOrb, 'speaking'); bubble.append(data?.text || ''); }
        else if (event === 'citation') bubble.addCitation(data, pid);
        else if (event === 'whisper') bubble.setWhisper(data?.text);
        else if (event === 'action_proposal') {
          const key = data?.id ? String(data.id) : `${data?.kind}:${data?.title || data?.body || ''}`;
          if (!seenProposals.has(key)) { seenProposals.add(key); const card = actionProposalCard(data, pid); if (card) actionCards.push(card); }
        } else if (event === 'perspective' && tier === 'gold') extras.push(perspectiveCard(data?.items || data?.perspectives, tier));
        else if (event === 'suggestion') extras.push(suggestionRow(data?.items, tier));
        else if (event === 'crisis') crisis = data;
        else if (event === 'error') gotError = data;
        else if (event === 'done') { receivedDone = true; if (data?.writebackQueued === true) writebackQueued = true; }
      },
    );
  } catch (err) {
    if (err?.name === 'AbortError') { bubble.flushCitations(); setOrbState(els.headerOrb, 'idle'); if (!bubble.started) bubble.el.remove(); return; }
    gotError = { code: err?.code, status: err?.status, message: err?.message };
  } finally {
    if (state.streaming === ctrl) state.streaming = null;
    syncComposerAction();
  }
  if (state.personId !== pid) return;
  if (crisis) {
    setOrbState(els.headerOrb, 'idle');
    bubble.el.replaceWith(crisisCard(crisis, { onContinue: () => stopCrisis() }));
    $('#chat').classList.add('is-crisis');
    return scrollDown();
  }
  if (!gotError && !receivedDone) gotError = { code: 'SSE_TRUNCATED', message: '网络中断，这段回复没有完整送达，再试一次？' };
  if (gotError) {
    setOrbState(els.headerOrb, 'idle');
    const msg = gotError.code === 'SSE_IDLE_TIMEOUT' ? '网络响应超时，轻点再试一次' : gotError.message || friendlyMessage(gotError.code, gotError.status);
    bubble.fail(msg, () => { bubble.el.remove(); stream(text, ids, attempt, userEl); });
    return scrollDown();
  }
  bubble.finishTrace();
  bubble.flushCitations();
  setOrbState(els.headerOrb, 'done');
  if (!bubble.started) bubble.setText('丘比特想了想，暂时没有更多可说的');
  if (writebackQueued) {
    if (userEl?.isConnected && !userEl.nextElementSibling?.classList.contains('chat-writeback-receipt')) userEl.after(writebackReceipt(tier));
    actions.reloadMaterials(pid);
  }
  for (const card of actionCards) els.inner.appendChild(card);
  for (const x of extras) if (x) els.inner.appendChild(x);
  scrollDown();
}

function send(overrideText) {
  const fromInput = overrideText === undefined;
  const text = (fromInput ? els.input.value : String(overrideText)).trim();
  if (!text || state.streaming || !state.personId) return;
  haptic('light');
  dismissTip();
  const attachments = attachedMaterials();
  const ids = attachments.map((m) => m.id);
  hideStarters();
  els.inner.querySelector('.ai-chat-empty')?.remove();
  els.inner.querySelector('.chat-empty-hint')?.remove();
  const userEl = userBubble(text, attachments);
  els.inner.appendChild(userEl);
  if (fromInput) { els.input.value = ''; autoGrow(); saveDraft(); }
  state.attached.clear();
  renderTray();
  drawerCtrl?.setOpen(false, { restoreFocus: false });
  const attempt = { clientMessageId: uuid(), idem: uuid() };
  state.liveClientIds.add(attempt.clientMessageId);
  stream(text, ids, attempt, userEl);
}

export function openChat(personId, opts = {}) {
  ensureStyle?.('./styles/chat.css');
  const s = store.get();
  let pid = personId || s.activePersonId;
  if (!pid || !s.people[pid]) pid = s.coreIds[0] || s.casualIds[0] || null;
  if (!pid) return whisper('先记下一个人，再和丘比特聊', 2600);
  if (!state.built) build();
  const root = $('#chat');
  clearTimeout(closeTimer);
  closeTimer = null;
  clearSwipeStyles(root);
  if (state.personId !== pid) { state.streaming?.abort(); voiceCtrl?.cancel(); state.attached.clear(); drawerCtrl?.resetPerson(); }
  state.personId = pid;
  state.liveClientIds.clear();
  const p = s.people[pid];
  const safeTier = Object.hasOwn(ARROW_SVGS, p.tier) ? p.tier : 'crystal';
  root.dataset.arrowTier = safeTier;
  if (els.titleName) els.titleName.textContent = p.nameCn || '丘比特';
  clear(els.arrow);
  const arrowSvg = svg(ARROW_SVGS[safeTier]);
  if (arrowSvg?.style) { arrowSvg.style.width = '14px'; arrowSvg.style.height = '14px'; arrowSvg.style.display = 'block'; arrowSvg.style.flexShrink = '0'; }
  els.arrow.appendChild(arrowSvg);
  setOrbState(els.headerOrb, 'idle');
  stopCrisis();
  if (opts?.materialId) {
    const mid = String(opts.materialId);
    if (!state.attached.has(mid) && state.attached.size >= 5) state.attached.delete(state.attached.values().next().value);
    state.attached.add(mid);
  }
  els.input.value = (s.chatDraftsByPerson || {})[pid] || '';
  autoGrow();
  renderTray();
  hideStarters();
  syncComposerAction();
  root.style.display = '';
  root.hidden = false;
  requestAnimationFrame(() => root.classList.add('is-active'));
  for (const id of ['journalCanvas', 'dock']) { const el = $(`#${id}`); if (el) el.inert = true; }
  const initialPrompt = typeof opts?.initialPrompt === 'string' ? opts.initialPrompt.trim() : '';
  loadHistory({ hasInitialPrompt: Boolean(initialPrompt) });
  if (!s.materials[pid]?.loaded) loadMaterials(pid).then(() => state.personId === pid && (renderTray(), drawerCtrl?.isOpen() && drawerCtrl.renderList()));
  if (initialPrompt) send(initialPrompt);
}

export function closeChat({ fromSwipe = false } = {}) {
  const root = $('#chat');
  if (!root || root.hidden) return;
  saveDraft();
  state.streaming?.abort();
  state.streaming = null;
  voiceCtrl?.cancel();
  state.historySeq += 1;
  drawerCtrl?.setOpen(false, { restoreFocus: false });
  root.classList.remove('is-active');
  for (const id of ['journalCanvas', 'dock']) { const el = $(`#${id}`); if (el) el.inert = false; }
  clearTimeout(closeTimer);
  closeTimer = setTimeout(() => {
    closeTimer = null;
    if (!root.classList.contains('is-active')) { clearSwipeStyles(root); if (els.inner) clear(els.inner); root.hidden = true; root.style.display = 'none'; }
  }, fromSwipe ? 220 : 240);
  $('#aiOrb')?.focus?.({ preventScroll: true });
}

export const sendToCupid = (personId, text) => openChat(personId, { initialPrompt: text });
