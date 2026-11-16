// chat.js — full-screen Muse chat with Cupid about one person. POST + SSE streaming,
// citation mini-polaroids that jump back to the page, right-edge drawer of real materials,
// IME-safe auto-growing composer, crisis card that stops the normal UI.
import { h, clear, svg, $ } from '../dom.js';
import { api, uuid } from '../api/endpoints.js';
import { friendlyMessage } from '../api/client.js';
import { store } from '../store.js';
import { BACK_SVG, SEND_SVG, PAPERS_SVG, ARROW_SVGS, CUPID_IMG } from '../icons.js';
import { shouldSendOnEnter, formatDateCn } from '../format.js';
import { userBubble, aiBubble, perspectiveCard, suggestionRow, crisisCard, historyMessage } from './chat-render.js';
import { loadMaterials } from './journal.js';
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

let state = { personId: null, attached: new Set(), streaming: null, built: false };
let els = {};

function build() {
  const root = $('#chat');
  clear(root);
  els.back = h('button', { type: 'button', class: 'muse-glass-circle-btn', 'aria-label': '回到手帐', onClick: () => closeChat() }, svg(BACK_SVG));
  els.drawerPill = h('button', { type: 'button', class: 'muse-drawer-pill', 'aria-label': '挑几页手帐给丘比特看', onClick: () => toggleDrawer(true) }, svg(PAPERS_SVG), h('span', { text: '手帐' }));
  els.medallion = h('div', { class: 'muse-cupid-medallion' }, h('img', { class: 'muse-medallion-img', src: CUPID_IMG, alt: '丘比特' }));
  els.arrow = h('span', { class: 'cupid-inline-arrow', 'aria-hidden': 'true' });
  els.sub = h('span', { class: 'muse-status-sub-row' });
  els.messages = h('div', { class: 'ai-chat-messages', role: 'log', 'aria-label': '和丘比特的对话' });
  els.edge = h('div', { class: 'chat-right-edge-gesture-zone', 'aria-hidden': 'true' });
  els.scrim = h('div', { class: 'chat-material-scrim', onClick: () => toggleDrawer(false) });
  els.drawerList = h('div', { class: 'drawer-materials-list' });
  els.drawerCount = h('span', { class: 'drawer-count-pill', text: '0' });
  els.drawer = h(
    'aside',
    { class: 'chat-material-drawer', 'aria-label': '挑几页手帐', 'aria-hidden': 'true' },
    h('div', { class: 'drawer-inner-header' }, h('div', {}, h('p', { class: 'drawer-title', text: '挑几页给丘比特看' }), h('p', { class: 'drawer-subtitle', text: '选中的会一起发过去' })), els.drawerCount),
    els.drawerList,
  );
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
        els.medallion,
        h('div', { class: 'muse-cupid-status-pill' }, h('span', { class: 'muse-status-main-row' }, h('span', { class: 'cupid-chat-name', text: '丘比特' }), els.arrow), els.sub),
      ),
    ),
    els.messages,
    els.edge,
    els.scrim,
    els.drawer,
    h('div', { class: 'ai-chat-composer-wrap' }, els.tray, h('div', { class: 'muse-composer-pill-bar' }, els.plus, els.input, els.send)),
  );
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
  root.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
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
  requestAnimationFrame(() => (els.messages.scrollTop = els.messages.scrollHeight));
}

function renderDrawer() {
  const items = store.get().materials[state.personId]?.items || [];
  clear(els.drawerList);
  els.drawerCount.textContent = String(state.attached.size);
  if (!items.length) {
    els.drawerList.appendChild(h('p', { class: 'drawer-empty', text: '这一页还没有手帐。先回去记一句吧。' }));
  }
  for (const m of items) {
    const selected = state.attached.has(m.id);
    els.drawerList.appendChild(
      h(
        'button',
        {
          type: 'button',
          class: ['drawer-mat-card', selected && 'is-selected'],
          'aria-pressed': String(selected),
          onClick: () => {
            if (state.attached.has(m.id)) state.attached.delete(m.id);
            else if (state.attached.size >= 5) return whisper('一次最多附 5 页', 2000);
            else state.attached.add(m.id);
            renderDrawer();
            renderTray();
          },
        },
        m.type === 'photo' && m.mediaUrl ? h('img', { class: 'drawer-mat-thumb', src: m.mediaUrl, alt: '' }) : h('span', { class: 'drawer-mat-icon-box', 'aria-hidden': 'true', text: m.type === 'voice' ? '♪' : '✎' }),
        h('span', { class: 'drawer-mat-meta' }, h('strong', { text: [m.title, formatDateCn(m.happenedOn)].filter(Boolean).join(' · ') || '一页手帐' }), h('small', { text: m.snippet || m.body || '' })),
        h('span', { class: 'drawer-mat-check', 'aria-hidden': 'true', text: '✓' }),
      ),
    );
  }
}

function attachedMaterials() {
  const items = store.get().materials[state.personId]?.items || [];
  return items.filter((m) => state.attached.has(m.id));
}

function renderTray() {
  clear(els.tray);
  const list = attachedMaterials();
  els.tray.hidden = !list.length;
  for (const m of list) {
    els.tray.appendChild(
      h(
        'span',
        { class: 'attached-chip' },
        h('span', { class: 'attached-chip-badge', 'aria-hidden': 'true', text: m.type === 'voice' ? '♪' : '✎' }),
        h('span', { class: 'attached-chip-title', text: m.title || m.snippet || '一页手帐' }),
        h('button', {
          type: 'button',
          class: 'attached-chip-remove',
          'aria-label': '不附这页了',
          text: '✕',
          onClick: () => {
            state.attached.delete(m.id);
            renderTray();
            renderDrawer();
          },
        }),
      ),
    );
  }
}

function toggleDrawer(open) {
  if (open) {
    if (!store.get().materials[state.personId]?.loaded) loadMaterials(state.personId).then(renderDrawer);
    renderDrawer();
  }
  els.drawer.classList.toggle('is-open', open);
  els.scrim.classList.toggle('is-open', open);
  els.drawer.setAttribute('aria-hidden', String(!open));
  if (open) els.drawer.querySelector('button')?.focus({ preventScroll: true });
}

async function loadHistory() {
  const p = person();
  clear(els.messages);
  const tier = p?.tier || 'crystal';
  try {
    const res = await api.chatHistory(state.personId, { limit: 40 });
    const items = res.items || [];
    if (!items.length) els.messages.appendChild(h('p', { class: 'chat-empty-hint', text: EMPTY_COPY[tier](p.nameCn) }));
    for (const m of items) els.messages.append(...historyMessage(m, state.personId, tier));
  } catch {
    els.messages.appendChild(h('p', { class: 'chat-empty-hint', text: EMPTY_COPY[tier](p.nameCn) }));
  }
  scrollDown();
}

function stopCrisis() {
  $('#chat').classList.remove('is-crisis');
}

async function stream(text, ids, attempt) {
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
  const extras = [];
  try {
    await api.streamChat(
      state.personId,
      { message: text, referencedMaterialIds: ids, clientMessageId: attempt.clientMessageId, idem: attempt.idem, signal: ctrl.signal },
      ({ event, data }) => {
        if (event === 'delta') bubble.append(data?.text || '');
        else if (event === 'citation') bubble.addCitation(data, state.personId);
        else if (event === 'whisper') bubble.setWhisper(data?.text);
        else if (event === 'perspective' && tier === 'gold') extras.push(perspectiveCard(data?.items));
        else if (event === 'suggestion') extras.push(suggestionRow(data?.items));
        else if (event === 'crisis') crisis = data;
        else if (event === 'error') gotError = data;
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
    state.streaming = null;
    els.send.disabled = !els.input.value.trim();
  }
  if (crisis) {
    bubble.el.replaceWith(crisisCard(crisis, { onContinue: () => stopCrisis() }));
    $('#chat').classList.add('is-crisis');
    scrollDown();
    return;
  }
  if (gotError) {
    bubble.fail(gotError.message || friendlyMessage(gotError.code, gotError.status), () => {
      bubble.el.remove();
      stream(text, ids, attempt); // same Idempotency-Key + clientMessageId → safe replay
    });
    scrollDown();
    return;
  }
  if (!bubble.started) bubble.setText('丘比特想了想，暂时没有更多可说的。');
  for (const x of extras) if (x) els.messages.appendChild(x);
  scrollDown();
}

function send() {
  const text = els.input.value.trim();
  if (!text || state.streaming || !state.personId) return;
  const attachments = attachedMaterials();
  const ids = attachments.map((m) => m.id);
  els.messages.querySelector('.chat-empty-hint')?.remove();
  els.messages.appendChild(userBubble(text, attachments));
  els.input.value = '';
  autoGrow();
  state.attached.clear();
  renderTray();
  toggleDrawer(false);
  stream(text, ids, { clientMessageId: uuid(), idem: uuid() });
}

export function openChat(personId) {
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
  state.personId = pid;
  const p = s.people[pid];
  const tier = p.tier || 'crystal';
  root.dataset.arrowTier = tier;
  clear(els.arrow);
  els.arrow.appendChild(svg(ARROW_SVGS[tier]));
  els.sub.textContent = SUB_COPY[tier](p.nameCn);
  stopCrisis();
  if (changed) {
    state.attached.clear();
    renderTray();
  }
  root.hidden = false;
  requestAnimationFrame(() => {
    root.classList.add('is-active');
    els.medallion.classList.remove('is-cupid-greeting');
    void els.medallion.offsetWidth;
    els.medallion.classList.add('is-cupid-greeting');
  });
  for (const id of ['journalCanvas', 'dock']) $(`#${id}`).inert = true;
  loadHistory();
  if (!s.materials[pid]?.loaded) loadMaterials(pid);
  setTimeout(() => els.input.focus({ preventScroll: true }), 260);
}

export function closeChat() {
  const root = $('#chat');
  if (root.hidden) return;
  state.streaming?.abort();
  toggleDrawer(false);
  root.classList.remove('is-active');
  for (const id of ['journalCanvas', 'dock']) $(`#${id}`).inert = false;
  setTimeout(() => {
    if (!root.classList.contains('is-active')) root.hidden = true;
  }, 340);
  $('#aiOrb')?.focus({ preventScroll: true });
}

/** Ask Cupid a first question programmatically (used by onboarding). Resolves with the bubble text. */
export function sendToCupid(personId, text) {
  openChat(personId);
  els.input.value = text;
  send();
}
