// chat-render.js — building blocks for Cupid's replies: bubbles, citation mini-polaroids,
// whisper notes, gold-only perspective card, suggestion chips, crisis card. All via textContent.
import { h } from '../dom.js';
import { citationLabel, formatClock } from '../format.js';
import { whisper } from './toast.js';
import { actions } from '../actions.js';

export function userBubble(text, attachments = []) {
  const b = h('div', { class: 'chat-bubble user' });
  if (attachments.length) {
    b.appendChild(
      h(
        'div',
        { class: 'bubble-multimodal-attachments' },
        ...attachments.map((m) =>
          h('span', { class: 'bubble-att-card' }, h('span', { class: 'bubble-att-badge-icon', 'aria-hidden': 'true', text: m.type === 'voice' ? '♪' : '✎' }), h('span', { class: 'bubble-att-label', text: m.title || m.snippet || '一页手帐' })),
        ),
      ),
    );
  }
  b.appendChild(document.createTextNode(text));
  return b;
}

/** An empty Cupid bubble with a typing shimmer until the first delta. */
export function aiBubble() {
  const textEl = h('span', { class: 'ai-text' });
  const shimmer = h('span', { class: 'typing-shimmer', 'aria-label': '丘比特正在想' }, h('i'), h('i'), h('i'));
  const bubble = h('div', { class: 'chat-bubble ai', 'aria-live': 'polite' }, shimmer, textEl);
  const api = {
    el: bubble,
    started: false,
    append(t) {
      if (!api.started) {
        shimmer.remove();
        api.started = true;
      }
      textEl.textContent += t;
    },
    setText(t) {
      shimmer.remove();
      api.started = true;
      textEl.textContent = t;
    },
    citations: null,
    addCitation(c, personId) {
      if (!api.citations) {
        api.citations = h('div', { class: 'cite-row', 'aria-label': '引用的手帐' });
        bubble.appendChild(api.citations);
      }
      if (api.citations.querySelector(`[data-material-id="${CSS.escape(String(c.materialId))}"]`)) return;
      api.citations.appendChild(citePill(c, personId));
    },
    setWhisper(t) {
      if (!t) return;
      bubble.appendChild(h('p', { class: 'chat-whisper-note', text: t }));
    },
    fail(message, onRetry) {
      shimmer.remove();
      bubble.classList.add('is-failed');
      if (!api.started) textEl.textContent = message;
      else bubble.appendChild(h('p', { class: 'chat-whisper-note', text: message }));
      if (onRetry) bubble.appendChild(h('button', { type: 'button', class: 'chat-retry-btn', text: '再试一次', onClick: onRetry }));
    },
  };
  return api;
}

export function citePill(c, personId) {
  const kind = c.type === 'voice' || /语音/.test(c.title || '') ? 'voice' : 'note';
  const pill = h(
    'button',
    {
      type: 'button',
      class: 'cite-pill',
      dataset: { kind, materialId: c.materialId },
      'aria-label': `翻到这页手帐：${citationLabel(c)}`,
      onClick: () => {
        actions.closeChat();
        actions.openPerson(personId, { materialId: c.materialId });
      },
    },
    h('span', { class: 'cite-thumb', 'aria-hidden': 'true', text: kind === 'voice' ? '♪' : '✎' }),
    h('span', { class: 'cite-label', text: citationLabel(c) }),
  );
  return pill;
}

export function perspectiveCard(items) {
  if (!items?.length) return null;
  return h('details', { class: 'perspective-card' }, h('summary', { text: '换个角度想想' }), h('ul', {}, ...items.map((t) => h('li', { text: t }))));
}

export function suggestionRow(items) {
  if (!items?.length) return null;
  return h(
    'div',
    { class: 'suggestion-row', 'aria-label': '可以这样说' },
    ...items.slice(0, 3).map((t) =>
      h(
        'div',
        { class: 'suggestion-chip' },
        h('span', { class: 'suggestion-text', text: t }),
        h('button', {
          type: 'button',
          class: 'suggestion-copy',
          text: '复制',
          'aria-label': `复制这句：${t}`,
          onClick: async () => {
            try {
              await navigator.clipboard.writeText(t);
              whisper('复制好了', 1600);
            } catch {
              whisper('没能复制，长按文字也可以', 2200);
            }
          },
        }),
      ),
    ),
  );
}

export function crisisCard(card, { onContinue } = {}) {
  const hotlines = card?.hotlines || [];
  return h(
    'section',
    { class: 'crisis-card', role: 'alert', 'aria-live': 'assertive' },
    h('h3', { text: card?.title || '你现在还好吗？' }),
    h('p', { text: card?.careMessage || '如果你正处在很难受的时刻，请先照顾好自己。你不是一个人。' }),
    hotlines.length
      ? h(
          'ul',
          { class: 'crisis-hotlines' },
          ...hotlines.map((l) =>
            h('li', {}, h('a', { href: `tel:${String(l.phone || '').replace(/[^\d+]/g, '')}` }, h('span', { text: l.name || '心理援助热线' }), h('span', { text: l.phone || '' }))),
          ),
        )
      : null,
    card?.disclaimer ? h('p', { class: 'crisis-disclaimer', text: card.disclaimer }) : null,
    onContinue ? h('button', { type: 'button', class: 'crisis-continue', text: '我现在安全，回到手帐', onClick: onContinue }) : null,
  );
}

export function timeDivider(iso) {
  const t = formatClock(iso);
  return t ? h('p', { class: 'muse-chat-time-divider', text: t }) : null;
}

/** Render a persisted message (history). */
export function historyMessage(m, personId, tier) {
  if (m.role === 'user') return [userBubble(m.content || '')];
  const b = aiBubble();
  b.setText(m.content || '');
  for (const c of m.citations || []) b.addCitation(c, personId);
  b.setWhisper(m.whisperNote);
  const out = [b.el];
  if (tier === 'gold') {
    const p = perspectiveCard(m.perspectives);
    if (p) out.push(p);
  }
  return out;
}
