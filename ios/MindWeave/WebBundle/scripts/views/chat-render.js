// chat-render.js — building blocks for Cupid's replies: bubbles, live streaming agent trace pill,
// executable 1-tap action cards, proactive care whisper card, compact tactile citation tokens,
// whisper notes, writeback receipt, gold-only perspective card, copyable suggestions, crisis card.
import { h, clear, svg } from '../dom.js';
import { api } from '../api/endpoints.js';
import { friendlyMessage } from '../api/client.js';
import { store } from '../store.js';
import { BELL_SVG, PEN_SVG, AUDIO_NOTE_SVG } from '../icons.js';
import { haptic, copyText } from '../native-bridge.js';
import { formatClock, formatDateCn, clip, todayIso, sanitizeAgentStep, summarizeAgentTrace, formatActionPreview } from '../format.js';
import { whisper } from './toast.js';
import { actions } from '../actions.js';

const UUID_RE = /[0-9a-f]{8}-[0-9a-f]{4}(?:-[0-9a-f]{4}){0,2}(?:-[0-9a-f]{12})?/gi;
const GENERIC_TITLES = new Set(['日常事件记录', '重要日子', '聊天随手备忘', '手帐便签', '日常随手记', '语音记录', '语音手帐', '随手记']);

function sanitizePreview(raw) {
  return String(raw || '').replace(UUID_RE, '').replace(/\s+/g, ' ').trim();
}

export function cleanMaterialLabel(m, max = 18) {
  const snippet = sanitizePreview(m?.snippet || m?.body);
  if (snippet) return clip(snippet, max);
  const title = sanitizePreview(m?.title);
  return title ? clip(title, max) : '一页手帐';
}

function lookupMaterial(personId, materialId) {
  if (!personId || !materialId) return null;
  return (store.get().materials[personId]?.items || []).find((m) => m.id === materialId) || null;
}

function kindSvg(kind) {
  return svg(kind === 'voice' ? AUDIO_NOTE_SVG : PEN_SVG, 'mw-mono-icon');
}

function citationParts(c, personId) {
  const mat = lookupMaterial(personId, c?.materialId);
  const rawTitle = sanitizePreview(c?.title);
  const meaningfulTitle = rawTitle && !GENERIC_TITLES.has(rawTitle) ? rawTitle : '';
  const raw = [c?.snippet, mat?.snippet, mat?.body, meaningfulTitle, rawTitle].map(sanitizePreview).find(Boolean) || '一页手帐';
  const excerpt = clip(raw, 14);
  const date = formatDateCn(c?.happenedOn || mat?.happenedOn || mat?.createdAt);
  const kind = c?.type === 'voice' || mat?.type === 'voice' || /语音/.test(c?.title || '') ? 'voice' : mat?.type === 'photo' ? 'photo' : 'note';
  const tier = store.get().people[personId]?.tier || 'crystal';
  const thumbUrl = kind === 'photo' && tier !== 'silver' && mat?.mediaUrl ? mat.mediaUrl : null;
  return { excerpt, date, kind, thumbUrl };
}

export function userBubble(text, attachments = []) {
  const b = h('div', { class: 'chat-bubble user' });
  if (attachments.length) {
    b.appendChild(
      h(
        'div',
        { class: 'bubble-multimodal-attachments' },
        ...attachments.map((m) =>
          h(
            'span',
            { class: 'bubble-att-card' },
            h('span', { class: 'bubble-att-badge-icon', 'aria-hidden': 'true' }, kindSvg(m.type === 'voice' ? 'voice' : 'note')),
            h('span', { class: 'bubble-att-label', text: cleanMaterialLabel(m, 16) }),
          ),
        ),
      ),
    );
  }
  b.appendChild(document.createTextNode(text));
  return b;
}

/** Whisper-quiet writeback receipt shown beneath a user bubble when Cupid filed a note. */
export function writebackReceipt() {
  return h('p', { class: 'chat-writeback-receipt', role: 'status' }, h('span', { class: 'writeback-spark', 'aria-hidden': 'true', text: '✦' }), h('span', { text: '已轻轻记进手帐' }));
}

/** Live streaming agent execution trace pill → settles into a whisper-quiet collapsible pill. */
export function agentTracePill() {
  const steps = [];
  let settled = false;
  const liveText = h('span', { class: 'agent-trace-live-text' });
  const liveEl = h('div', { class: 'agent-trace-live', role: 'status', 'aria-live': 'polite' }, h('span', { class: 'agent-trace-spark is-pulsing', 'aria-hidden': 'true', text: '✦' }), liveText);
  const summaryText = h('span', { class: 'agent-trace-summary-text' });
  const listEl = h('ol', { class: 'agent-trace-steps' });
  const summaryEl = h(
    'summary',
    { class: 'agent-trace-summary', onClick: () => haptic('selection') },
    h('span', { class: 'agent-trace-spark', 'aria-hidden': 'true', text: '✦' }),
    summaryText,
    h('span', { class: 'agent-trace-caret', 'aria-hidden': 'true', text: '▾' }),
  );
  const detailsEl = h('details', { class: 'agent-trace-pill', hidden: true }, summaryEl, listEl);
  const wrap = h('div', { class: 'agent-trace-wrap', hidden: true }, liveEl, detailsEl);

  function renderSteps() {
    clear(listEl);
    for (const s of steps) {
      listEl.appendChild(
        h(
          'li',
          { class: 'agent-trace-step-item' },
          h('span', { class: 'agent-trace-step-dot', 'aria-hidden': 'true', text: '·' }),
          h('span', { class: 'agent-trace-step-copy' }, h('span', { class: 'agent-trace-step-label', text: s.label }), s.detail ? h('span', { class: 'agent-trace-step-detail', text: s.detail }) : null),
        ),
      );
    }
    summaryText.textContent = summarizeAgentTrace(steps);
  }

  return {
    el: wrap,
    hasSteps: () => steps.length > 0,
    addStep(raw) {
      const clean = sanitizeAgentStep(raw);
      if (!clean) return;
      const idx = steps.findIndex((s) => (clean.step !== null && s.step === clean.step && clean.phase && s.phase === clean.phase) || s.label === clean.label);
      if (idx >= 0) steps[idx] = clean;
      else steps.push(clean);
      wrap.hidden = false;
      liveText.textContent = clean.label;
      if (settled) renderSteps();
    },
    settle() {
      if (!steps.length) return (wrap.hidden = true);
      settled = true;
      renderSteps();
      liveEl.hidden = true;
      detailsEl.hidden = false;
      wrap.classList.add('is-settled');
    },
  };
}

/** An empty Cupid bubble with live agent trace pill + typing shimmer until the first delta. */
export function aiBubble() {
  const trace = agentTracePill();
  const textEl = h('span', { class: 'ai-text' });
  const shimmer = h('span', { class: 'typing-shimmer', 'aria-label': '丘比特正在想' }, h('i'), h('i'), h('i'));
  const bubble = h('div', { class: 'chat-bubble ai', 'aria-live': 'polite' }, trace.el, shimmer, textEl);
  const ctrl = {
    el: bubble,
    started: false,
    citations: null,
    addAgentStep(step) {
      trace.addStep(step);
      if (!ctrl.started && trace.hasSteps()) shimmer.hidden = true;
    },
    finishTrace() {
      trace.settle();
    },
    append(t) {
      if (!ctrl.started) {
        shimmer.remove();
        trace.settle();
        ctrl.started = true;
      }
      textEl.textContent += t;
    },
    setText(t) {
      shimmer.remove();
      trace.settle();
      ctrl.started = true;
      textEl.textContent = t;
    },
    addCitation(c, personId) {
      if (!c?.materialId) return;
      if (!ctrl.citations) {
        ctrl.citations = h('div', { class: 'cite-row', 'aria-label': '引用的手帐' });
        bubble.appendChild(ctrl.citations);
      }
      if (ctrl.citations.querySelector(`[data-material-id="${CSS.escape(String(c.materialId))}"]`)) return;
      ctrl.citations.appendChild(citePill(c, personId));
    },
    setWhisper(t) {
      if (t) bubble.appendChild(h('p', { class: 'chat-whisper-note', text: t }));
    },
    fail(message, onRetry) {
      shimmer.remove();
      trace.settle();
      bubble.classList.add('is-failed');
      if (!ctrl.started) textEl.textContent = message;
      else bubble.appendChild(h('p', { class: 'chat-whisper-note', text: message }));
      if (onRetry) bubble.appendChild(h('button', { type: 'button', class: 'chat-retry-btn', text: '再试一次', onClick: () => (haptic('light'), onRetry()) }));
    },
  };
  return ctrl;
}

/** 1-tap executable agent action card (create_reminder | save_note) rendered below Cupid's bubble. */
export function actionProposalCard(proposal, personId) {
  const kind = proposal?.kind;
  if (!personId || (kind !== 'create_reminder' && kind !== 'save_note')) return null;
  const isReminder = kind === 'create_reminder';
  const label = sanitizePreview(proposal.label) || (isReminder ? '顺手设个提醒' : '顺手存进手帐');
  const displayTitle = formatActionPreview(proposal.title || proposal.body);
  const rawBody = String(proposal.body || proposal.title || '').replace(UUID_RE, '').trim();
  if (!displayTitle || !rawBody) return null;
  const whenLabel = sanitizePreview(proposal.whenLabel) || formatDateCn(proposal.triggerDate);
  const idleBtnLabel = isReminder ? '设好提醒' : '存进手帐';
  let busy = false;
  let done = false;

  const btn = h('button', { type: 'button', class: 'action-proposal-btn', text: idleBtnLabel, 'aria-label': `${idleBtnLabel}：${displayTitle}` });
  const card = h(
    'div',
    { class: 'action-proposal-card', dataset: { kind }, role: 'region', 'aria-label': label },
    h('span', { class: 'action-proposal-orb', dataset: { kind }, 'aria-hidden': 'true' }, svg(isReminder ? BELL_SVG : PEN_SVG, 'action-orb-svg')),
    h('div', { class: 'action-proposal-body' }, h('p', { class: 'action-proposal-label', text: label }), h('p', { class: 'action-proposal-title', text: displayTitle }), whenLabel ? h('p', { class: 'action-proposal-when', text: whenLabel }) : null),
    btn,
  );

  btn.addEventListener('click', async () => {
    if (busy || done) return;
    busy = true;
    haptic('light');
    btn.disabled = true;
    btn.classList.add('is-loading');
    btn.textContent = '记下中…';
    try {
      if (isReminder) {
        const payload = { title: displayTitle, triggerDate: proposal.triggerDate || todayIso(), rule: proposal.rule || 'once' };
        if (proposal.factId) payload.factId = proposal.factId;
        if (proposal.leadDays != null) payload.leadDays = proposal.leadDays;
        await api.createReminder(personId, payload);
        btn.textContent = '✓ 已设好提醒';
        whisper('提醒已记进日程', 2200);
      } else {
        await api.createMaterial(personId, {
          kind: 'note',
          title: formatActionPreview(proposal.title) || '客观时间线要点',
          body: rawBody,
          pinned: false,
        });
        btn.textContent = '✓ 已存入手帐';
        whisper('已存入 TA 的手帐', 2200);
      }
      done = true;
      haptic('success');
      card.classList.add('is-done');
      btn.classList.remove('is-loading');
      btn.classList.add('is-done');
      actions.reloadMaterials(personId);
    } catch (err) {
      busy = false;
      haptic('warning');
      btn.disabled = false;
      btn.classList.remove('is-loading');
      btn.textContent = idleBtnLabel;
      whisper(err?.message || friendlyMessage(err?.code, err?.status) || '刚刚没存上，再点一次试试', 2600);
    }
  });
  return card;
}

/** Serene frosted Proactive Care Whisper Card shown on a fresh Cupid thread. */
export function proactiveBriefCard(brief, tier, onAsk) {
  const headline = sanitizePreview(brief?.headline);
  if (!headline) return null;
  const subline = sanitizePreview(brief?.subline);
  const prompt = String(brief?.prompt || '').trim() || headline;
  return h(
    'section',
    { class: 'cupid-brief-card', dataset: { tier: tier || 'crystal' }, 'aria-label': '丘比特的小提醒' },
    h(
      'div',
      { class: 'cupid-brief-copy' },
      h('p', { class: 'cupid-brief-headline' }, h('span', { class: 'cupid-brief-spark', 'aria-hidden': 'true', text: '✦' }), h('span', { text: headline })),
      subline ? h('p', { class: 'cupid-brief-subline', text: subline }) : null,
    ),
    h('button', { type: 'button', class: 'cupid-brief-cta', text: '问丘比特 →', 'aria-label': `问丘比特：${headline}`, onClick: () => (haptic('light'), onAsk?.(prompt)) }),
  );
}

export function citePill(c, personId) {
  const { excerpt, date, kind, thumbUrl } = citationParts(c, personId);
  const ariaText = date ? `${excerpt} · ${date}` : excerpt;
  const thumb = thumbUrl
    ? h('span', { class: 'cite-thumb is-photo', 'aria-hidden': 'true' }, h('img', { src: thumbUrl, alt: '' }))
    : h('span', { class: 'cite-thumb', 'aria-hidden': 'true' }, kindSvg(kind));
  return h(
    'button',
    {
      type: 'button',
      class: 'cite-pill',
      dataset: { kind, materialId: c.materialId },
      'aria-label': `翻到这页手帐：${ariaText}`,
      onClick: () => {
        haptic('light');
        actions.closeChat();
        actions.openPerson(personId, { materialId: c.materialId });
      },
    },
    thumb,
    h('span', { class: 'cite-label' }, h('span', { class: 'cite-excerpt', text: excerpt }), date ? h('span', { class: 'cite-date', text: date }) : null),
    h('span', { class: 'cite-jump-cue', 'aria-hidden': 'true', text: '↗' }),
  );
}

/** Gold-only perspective card. Strictly returns null for silver and crystal tiers. */
export function perspectiveCard(items, tier = 'gold') {
  if (tier !== 'gold' || !items?.length) return null;
  return h('details', { class: 'perspective-card' }, h('summary', { text: '换个角度想想', onClick: () => haptic('selection') }), h('ul', {}, ...items.map((t) => h('li', { text: t }))));
}

export function suggestionRow(items) {
  if (!items?.length) return null;
  return h(
    'div',
    { class: 'suggestion-row', 'aria-label': '可以这样说' },
    ...items.slice(0, 3).map((t) => {
      let resetTimer = null;
      const copyBtn = h('button', {
        type: 'button',
        class: 'suggestion-copy',
        text: '复制',
        'aria-label': `复制这句：${t}`,
        onClick: async () => {
          try {
            const ok = await copyText(t);
            if (ok === false) throw new Error('copy_failed');
            haptic('success');
            copyBtn.textContent = '已复制 ✓';
            copyBtn.classList.add('is-copied');
            clearTimeout(resetTimer);
            resetTimer = setTimeout(() => {
              copyBtn.textContent = '复制';
              copyBtn.classList.remove('is-copied');
            }, 1800);
            whisper('复制好了', 1600);
          } catch {
            haptic('warning');
            whisper('没能复制，长按文字也可以', 2200);
          }
        },
      });
      return h('div', { class: 'suggestion-chip' }, h('span', { class: 'suggestion-text', text: t }), copyBtn);
    }),
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
      ? h('ul', { class: 'crisis-hotlines' }, ...hotlines.map((l) => h('li', {}, h('a', { href: `tel:${String(l.phone || '').replace(/[^\d+]/g, '')}` }, h('span', { text: l.name || '心理援助热线' }), h('span', { text: l.phone || '' })))))
      : null,
    card?.disclaimer ? h('p', { class: 'crisis-disclaimer', text: card.disclaimer }) : null,
    onContinue ? h('button', { type: 'button', class: 'crisis-continue', text: '我现在安全，回到手帐', onClick: () => (haptic('light'), onContinue()) }) : null,
  );
}

export function timeDivider(iso) {
  const t = formatClock(iso);
  return t ? h('p', { class: 'muse-chat-time-divider', text: t }) : null;
}

/** Render a persisted message (history). Guarantees silver & crystal never render perspectiveCard. */
export function historyMessage(m, personId, tier) {
  if (m.role === 'user') return [userBubble(m.content || '')];
  const b = aiBubble();
  b.setText(m.content || '');
  for (const c of m.citations || []) b.addCitation(c, personId);
  b.setWhisper(m.whisperNote);
  const out = [b.el];
  if (tier === 'gold') {
    const p = perspectiveCard(m.perspectives, tier);
    if (p) out.push(p);
  }
  return out;
}

const STARTER_PROMPTS = {
  gold: ['最近有什么要替 TA 留意的？', '结合手帐，帮我挑份贴心小礼物', '帮我理理和 TA 最近的细节'],
  silver: ['按时间线帮我理清客观事实', '起草一封不卑不亢的书面留痕', '提醒我守好哪些边界'],
  crystal: ['整理一下和 TA 的待办与礼数', '最近记了哪些要点？'],
};

/** Tier-aware frosted starter prompt chips for empty or post-onboarding threads. */
export function starterChips(tier, onPick) {
  const prompts = STARTER_PROMPTS[tier] || STARTER_PROMPTS.crystal;
  return prompts.map((prompt, idx) =>
    h(
      'button',
      { type: 'button', class: 'cupid-starter-chip', style: { '--stagger': `${idx * 48}ms` }, onClick: () => (haptic('light'), onPick(prompt)) },
      h('span', { class: 'cupid-starter-dot', 'aria-hidden': 'true', text: '✦' }),
      h('span', { text: prompt }),
    ),
  );
}

/** Clean snippet + date card for the right-edge material drawer (no category title pills). */
export function drawerCard(m, { selected, tier, onToggle }) {
  const snippet = cleanMaterialLabel(m, 64);
  const date = formatDateCn(m.happenedOn || m.createdAt);
  const dateMeta = m.type === 'voice' && m.dur ? [date, m.dur].filter(Boolean).join(' · ') : date || (m.type === 'voice' ? '语音' : '手帐');
  const showPhoto = m.type === 'photo' && m.mediaUrl && tier !== 'silver';
  return h(
    'button',
    { type: 'button', class: ['drawer-mat-card', selected && 'is-selected'], 'aria-pressed': String(selected), onClick: onToggle },
    showPhoto
      ? h('img', { class: 'drawer-mat-thumb', src: m.mediaUrl, alt: '' })
      : h('span', { class: 'drawer-mat-icon-box', dataset: { kind: m.type === 'voice' ? 'voice' : 'note' }, 'aria-hidden': 'true' }, kindSvg(m.type === 'voice' ? 'voice' : 'note')),
    h('span', { class: 'drawer-mat-meta' }, h('span', { class: 'drawer-mat-snippet', text: snippet }), h('span', { class: 'drawer-mat-date', text: dateMeta })),
    h('span', { class: 'drawer-mat-check', 'aria-hidden': 'true', text: '✓' }),
  );
}

/** Attached slip pill displayed in the composer tray. */
export function attachedChip(m, onRemove) {
  return h(
    'span',
    { class: 'attached-chip' },
    h('span', { class: 'attached-chip-badge', 'aria-hidden': 'true' }, kindSvg(m.type === 'voice' ? 'voice' : 'note')),
    h('span', { class: 'attached-chip-title', text: cleanMaterialLabel(m, 16) }),
    h('button', { type: 'button', class: 'attached-chip-remove', 'aria-label': '不附这页了', text: '✕', onClick: () => (haptic('light'), onRemove()) }),
  );
}
