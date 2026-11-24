// chat-render.js — Gleb Porcelain Cupid message primitives (01e / 01l / 03e / BC3 / BC6).
// Borderless \n-split <p class="ai-para">, post-done citations w/ Quick-Verify popover,
// 1-row 52px action card w/ CoreHaptics memorySeal, Gold-only perspectives, persisted suggestions,
// Silver localOnly clipboard privacy, zero sparkle glyphs, zero generic pronouns.
import { h, clear, svg } from '../dom.js';
import { api } from '../api/endpoints.js';
import { friendlyMessage } from '../api/client.js';
import { store } from '../store.js';
import { BELL_SVG, PEN_SVG, AUDIO_NOTE_SVG } from '../icons.js';
import { haptic, copyText, mwNativeUI } from '../native-bridge.js';
import { formatClock, formatDateCn, clip, todayIso, sanitizeAgentStep, summarizeAgentTrace, formatActionPreview } from '../format.js';
import { whisper } from './toast.js';
import { actions } from '../actions.js';
import { crisisCard } from './crisis.js';

export { crisisCard };

const UUID_RE = /[0-9a-f]{8}-[0-9a-f]{4}(?:-[0-9a-f]{4}){0,2}(?:-[0-9a-f]{12})?/gi;
const URL_RE = /(https?:\/\/[^\s<>)\]}'"，。！？；：]+)/g;
const GENERIC_TITLES = new Set(['日常事件记录', '重要日子', '聊天随手备忘', '手帐便签', '日常随手记', '语音记录', '语音手帐', '随手记']);

const sanitizePreview = (raw) => String(raw || '').replace(UUID_RE, '').replace(/\s+/g, ' ').trim();
const kindSvg = (kind) => svg(kind === 'voice' ? AUDIO_NOTE_SVG : PEN_SVG, 'mw-mono-icon');
const lookupMaterial = (pid, mid) => (pid && mid ? (store.get().materials[pid]?.items || []).find((m) => m.id === mid) || null : null);

export function cleanMaterialLabel(m, max = 18) {
  const snippet = sanitizePreview(m?.snippet || m?.body);
  if (snippet) return clip(snippet, max);
  const title = sanitizePreview(m?.title);
  return title ? clip(title, max) : '一页手帐';
}

function citationParts(c, personId) {
  const mat = lookupMaterial(personId, c?.materialId);
  const rawTitle = sanitizePreview(c?.title);
  const meaningful = rawTitle && !GENERIC_TITLES.has(rawTitle) ? rawTitle : '';
  const full = [c?.snippet, mat?.body, mat?.snippet, meaningful, rawTitle].map(sanitizePreview).find(Boolean) || '一页手帐';
  const date = formatDateCn(c?.happenedOn || mat?.happenedOn || mat?.createdAt);
  const kind = c?.type === 'voice' || mat?.type === 'voice' || /语音/.test(c?.title || '') ? 'voice' : mat?.type === 'photo' ? 'photo' : 'note';
  const tier = store.get().people[personId]?.tier || 'crystal';
  const thumbUrl = kind === 'photo' && tier !== 'silver' && mat?.mediaUrl ? mat.mediaUrl : null;
  return { excerpt: clip(full, 14), full: clip(full, 140), date, kind, thumbUrl };
}

function appendRichText(el, line) {
  let last = 0;
  URL_RE.lastIndex = 0;
  let m;
  while ((m = URL_RE.exec(line))) {
    if (m.index > last) el.appendChild(document.createTextNode(line.slice(last, m.index)));
    const url = m[1];
    el.appendChild(h('a', {
      class: 'ai-ext-link', href: url, rel: 'noopener noreferrer', text: url,
      onClick: (ev) => (ev.preventDefault(), haptic('light'), mwNativeUI?.openSafari?.({ url })),
    }));
    last = m.index + url.length;
  }
  if (last < line.length) el.appendChild(document.createTextNode(line.slice(last)));
}

function renderProseInto(container, text) {
  clear(container);
  for (const line of String(text || '').split(/\n+/).map((s) => s.trim()).filter(Boolean)) {
    const p = h('p', { class: 'ai-para' });
    appendRichText(p, line);
    container.appendChild(p);
  }
}

export function userBubble(text, attachments = []) {
  const b = h('div', { class: 'chat-bubble user' });
  if (attachments.length) {
    b.appendChild(h('div', { class: 'bubble-multimodal-attachments' }, ...attachments.map((m) =>
      h('span', { class: 'bubble-att-card' },
        h('span', { class: 'bubble-att-badge-icon', 'aria-hidden': 'true' }, kindSvg(m.type === 'voice' ? 'voice' : 'note')),
        h('span', { class: 'bubble-att-label', text: cleanMaterialLabel(m, 16) }),
      ),
    )));
  }
  b.appendChild(document.createTextNode(text));
  return b;
}

/** Tier-aware quiet writeback receipt (zero sparkle glyph, factual register on Silver). */
export function writebackReceipt(tier = 'gold') {
  return h('p', { class: 'chat-writeback-receipt', role: 'status' },
    h('span', { class: 'mw-dot', 'aria-hidden': 'true' }),
    h('span', { text: tier === 'silver' ? '已按事实留档' : '已轻轻记进手帐' }),
  );
}

/** Live streaming agent trace pill settling into a quiet collapsible summary. */
export function agentTracePill() {
  const steps = [];
  let settled = false;
  const liveText = h('span', { class: 'agent-trace-live-text' });
  const liveEl = h('div', { class: 'agent-trace-live', role: 'status', 'aria-live': 'polite' }, h('span', { class: 'mw-dot is-pulsing', 'aria-hidden': 'true' }), liveText);
  const summaryText = h('span', { class: 'agent-trace-summary-text' });
  const listEl = h('ol', { class: 'agent-trace-steps' });
  const summaryEl = h('summary', { class: 'agent-trace-summary', onClick: () => haptic('selection') }, h('span', { class: 'mw-dot', 'aria-hidden': 'true' }), summaryText, h('span', { class: 'agent-trace-caret', 'aria-hidden': 'true', text: '▾' }));
  const detailsEl = h('details', { class: 'agent-trace-pill', hidden: true }, summaryEl, listEl);
  const wrap = h('div', { class: 'agent-trace-wrap', hidden: true }, liveEl, detailsEl);

  function renderSteps() {
    clear(listEl);
    for (const s of steps) {
      listEl.appendChild(h('li', { class: 'agent-trace-step-item' },
        h('span', { class: 'agent-trace-step-dot', 'aria-hidden': 'true', text: '·' }),
        h('span', { class: 'agent-trace-step-copy' }, h('span', { class: 'agent-trace-step-label', text: s.label }), s.detail ? h('span', { class: 'agent-trace-step-detail', text: s.detail }) : null),
      ));
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

/** Borderless Gleb AI turn: trace pill + \n-split <p class="ai-para"> + post-done inline citations. */
export function aiBubble() {
  const trace = agentTracePill();
  const proseEl = h('div', { class: 'ai-text ai-prose' });
  const shimmer = h('span', { class: 'typing-shimmer', 'aria-label': '丘比特正在想' }, h('i'), h('i'), h('i'));
  const bubble = h('div', { class: 'chat-bubble ai', 'aria-live': 'polite' }, trace.el, shimmer, proseEl);
  let rawText = '';
  const pendingCitations = [];
  let citationsFlushed = false;

  function mountCitation(c, personId) {
    if (!c?.materialId) return;
    if (!ctrl.citations) {
      ctrl.citations = h('div', { class: 'cite-row', 'aria-label': '引用的手帐' });
      bubble.appendChild(ctrl.citations);
    }
    const esc = typeof CSS !== 'undefined' && CSS.escape ? CSS.escape(String(c.materialId)) : String(c.materialId);
    if (ctrl.citations.querySelector(`[data-material-id="${esc}"]`)) return;
    ctrl.citations.appendChild(citePill(c, personId));
  }

  const ctrl = {
    el: bubble,
    started: false,
    citations: null,
    getText: () => rawText,
    addAgentStep(step) { trace.addStep(step); if (!ctrl.started && trace.hasSteps()) shimmer.hidden = true; },
    finishTrace() { trace.settle(); },
    append(t) {
      if (!ctrl.started) { shimmer.remove ? shimmer.remove() : (shimmer.hidden = true); trace.settle(); ctrl.started = true; }
      rawText += String(t || '');
      renderProseInto(proseEl, rawText);
    },
    setText(t) { shimmer.remove ? shimmer.remove() : (shimmer.hidden = true); trace.settle(); ctrl.started = true; rawText = String(t || ''); renderProseInto(proseEl, rawText); },
    addCitation(c, personId, { immediate = false } = {}) {
      if (!c?.materialId) return;
      if (immediate || citationsFlushed) mountCitation(c, personId);
      else pendingCitations.push({ c, personId });
    },
    flushCitations() {
      citationsFlushed = true;
      while (pendingCitations.length) {
        const next = pendingCitations.shift();
        mountCitation(next.c, next.personId);
      }
    },
    setWhisper(t) { if (t) bubble.appendChild(h('p', { class: 'chat-whisper-note', text: t })); },
    fail(message, onRetry) {
      if (shimmer.remove) shimmer.remove();
      else shimmer.hidden = true;
      trace.settle();
      ctrl.flushCitations();
      bubble.classList.add('is-failed');
      if (!ctrl.started) renderProseInto(proseEl, message);
      else bubble.appendChild(h('p', { class: 'chat-whisper-note', text: message }));
      if (onRetry) bubble.appendChild(h('button', { type: 'button', class: 'chat-retry-btn', text: '再试一次', onClick: () => (haptic('light'), onRetry()) }));
    },
  };
  return ctrl;
}

/** Strict 1-row 52px action card (BC6 onceDate on createReminder + CoreHaptics memorySeal). */
export function actionProposalCard(proposal, personId) {
  const kind = proposal?.kind;
  if (!personId || (kind !== 'create_reminder' && kind !== 'save_note')) return null;
  const person = store.get().people[personId] || null;
  const tier = person?.tier || 'crystal';
  const personLabel = person?.nameCn ? `「${person.nameCn}」的` : '';
  const isReminder = kind === 'create_reminder';
  const displayTitle = formatActionPreview(proposal.title || proposal.body);
  const rawBody = String(proposal.body || proposal.title || '').replace(UUID_RE, '').trim();
  if (!displayTitle || !rawBody) return null;
  const whenLabel = sanitizePreview(proposal.whenLabel) || formatDateCn(proposal.onceDate || proposal.triggerDate);
  const idleBtnLabel = isReminder ? '设好提醒' : tier === 'silver' ? '留档' : '记下';
  let busy = false;
  let done = false;

  const btn = h('button', { type: 'button', class: 'action-proposal-btn chat-action-btn', text: idleBtnLabel, 'aria-label': `${idleBtnLabel}：${displayTitle}` });
  const card = h('div', { class: 'action-row-card action-proposal-card', dataset: { kind, tier }, role: 'region', 'aria-label': sanitizePreview(proposal.label) || idleBtnLabel },
    h('span', { class: 'action-proposal-orb', dataset: { kind }, 'aria-hidden': 'true' }, svg(isReminder ? BELL_SVG : PEN_SVG, 'action-orb-svg')),
    h('div', { class: 'action-proposal-body' }, h('p', { class: 'action-proposal-title', text: displayTitle }), whenLabel ? h('p', { class: 'action-proposal-when', text: whenLabel }) : null),
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
        // BC6 §2.9: live FastAPI CreatePersonReminderRequest requires `onceDate` (forbids `triggerDate`)
        const payload = { title: displayTitle, onceDate: proposal.onceDate || proposal.triggerDate || todayIso(), rule: proposal.rule || 'once' };
        if (proposal.factId) payload.factId = proposal.factId;
        if (proposal.leadDays != null) payload.leadDays = proposal.leadDays;
        await api.createReminder(personId, payload);
        btn.textContent = '已设好';
        whisper('提醒已记进日程', 2200);
      } else {
        await api.createMaterial(personId, { kind: 'note', title: formatActionPreview(proposal.title) || '客观时间线要点', body: rawBody, pinned: false });
        btn.textContent = tier === 'silver' ? '已留档' : '已记入';
        whisper(tier === 'silver' ? `已留档到${personLabel}边界记录` : `已存入${personLabel}手帐`, 2200);
      }
      done = true;
      haptic('memorySeal', { tier });
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

/** Inline Quick-Verify Citation Chip + Popover (open-webui #15204) with in-place Peek or journal jump. */
export function citePill(c, personId) {
  const { excerpt, full, date, kind, thumbUrl } = citationParts(c, personId);
  const ariaText = date ? `${excerpt} · ${date}` : excerpt;
  const thumb = thumbUrl
    ? h('span', { class: 'cite-thumb is-photo', 'aria-hidden': 'true' }, h('img', { src: thumbUrl, alt: '' }))
    : h('span', { class: 'cite-thumb', 'aria-hidden': 'true' }, kindSvg(kind));

  let popover = null;
  const openFullTarget = () => {
    const person = store.get().people[personId] || null;
    if (typeof actions.openPeek === 'function' && actions.openPeek.name !== 'noop') actions.openPeek(c.materialId, person, { fromChat: true });
    else { actions.closeChat(); actions.openPerson(personId, { materialId: c.materialId }); }
  };

  return h('button', {
    type: 'button',
    class: 'cite-chip cite-pill',
    dataset: { kind, materialId: c.materialId },
    'aria-label': `核对手帐出处：${ariaText}`,
    onClick: (ev) => {
      haptic('light');
      if (popover?.isConnected) { popover.remove(); popover = null; return; }
      popover = h('div', { class: 'cite-popover', role: 'dialog', 'aria-label': '原文核对' },
        date ? h('p', { class: 'cite-popover-date', text: date }) : null,
        h('p', { class: 'cite-popover-body', text: full }),
        h('div', { class: 'cite-popover-actions' },
          h('button', { type: 'button', class: 'cite-popover-open', text: '在手帐里查看 ↗', onClick: () => (haptic('light'), popover?.remove(), (popover = null), openFullTarget()) }),
          h('button', { type: 'button', class: 'cite-popover-close', text: '收起', onClick: () => (popover?.remove(), (popover = null)) }),
        ),
      );
      ev.currentTarget.after(popover);
    },
  }, thumb, h('span', { class: 'cite-label' }, date ? h('span', { class: 'cite-date', text: date }) : null, date ? h('span', { class: 'cite-sep', 'aria-hidden': 'true', text: '·' }) : null, h('span', { class: 'cite-excerpt', text: excerpt })), h('span', { class: 'cite-jump-cue', 'aria-hidden': 'true', text: '↗' }));
}

/** Gold-only perspective card. Strictly returns null for Silver and Crystal tiers. */
export function perspectiveCard(items, tier = 'gold') {
  if (tier !== 'gold' || !items?.length) return null;
  return h('details', { class: 'perspective-card' }, h('summary', { text: '换个角度想想', onClick: () => haptic('selection') }), h('ul', {}, ...items.map((t) => h('li', { text: t }))));
}

/** Copyable suggested reply chips (Silver tier uses localOnly clipboard so iCloud Universal Clipboard never syncs). */
export function suggestionRow(items, tier = 'crystal') {
  if (!items?.length) return null;
  return h('div', { class: 'suggestion-row', 'aria-label': '可以这样说' }, ...items.slice(0, 3).map((t) => {
    let resetTimer = null;
    const copyBtn = h('button', {
      type: 'button', class: 'suggestion-copy', text: '复制', 'aria-label': `复制这句：${t}`,
      onClick: async () => {
        try {
          const ok = tier === 'silver' && mwNativeUI?.copySensitive ? await mwNativeUI.copySensitive(t) : await copyText(t, { localOnly: tier === 'silver' });
          if (ok === false) throw new Error('copy_failed');
          haptic('success');
          copyBtn.textContent = '已复制';
          copyBtn.classList.add('is-copied');
          clearTimeout(resetTimer);
          resetTimer = setTimeout(() => { copyBtn.textContent = '复制'; copyBtn.classList.remove('is-copied'); }, 1800);
          whisper(tier === 'silver' ? '已仅在本机复制' : '复制好了', 1600);
        } catch {
          haptic('warning');
          whisper('没能复制，长按文字也可以', 2200);
        }
      },
    });
    return h('div', { class: 'suggestion-chip' }, h('span', { class: 'suggestion-text', text: t }), copyBtn);
  }));
}

export function timeDivider(iso) {
  const t = formatClock(iso);
  return t ? h('p', { class: 'muse-chat-time-divider', text: t }) : null;
}

/** Render persisted message (BC3 BUG-CC-10 suggestions + BC6 `m.perspectives` Gold-only). */
export function historyMessage(m, personId, tier) {
  if (m.role === 'user') return [userBubble(m.content || '')];
  const b = aiBubble();
  b.setText(m.content || '');
  for (const c of m.citations || []) b.addCitation(c, personId, { immediate: true });
  b.setWhisper(m.whisperNote);
  const out = [b.el];
  const currentTier = store.get().people[personId]?.tier || tier || 'crystal';
  if (currentTier === 'gold') {
    const list = Array.isArray(m.perspectives) ? m.perspectives : m.perspectiveCard?.items;
    const p = perspectiveCard(list, currentTier);
    if (p) out.push(p);
  }
  const suggs = suggestionRow(m.suggestions, currentTier);
  if (suggs) out.push(suggs);
  return out;
}

/** Tier-specific & name-personalized porcelain starter prompt chips (Dim 3.1). */
export function starterChips(tier, onPick, personName = '') {
  const n = String(personName || '').trim();
  const byTier = {
    gold: n
      ? [`最近有哪些要替「${n}」留意的小事？`, `结合手帐，帮我给「${n}」挑份贴心小礼物`, `帮我理一理和「${n}」最近的相处记录`]
      : ['最近有哪些值得留意的小事？', '结合手帐，帮我挑份贴心小礼物', '帮我理一理最近的相处记录'],
    silver: ['按时间线帮我理清客观事实', '起草一封冷静克制的书面留痕', '提醒我接下来要守好哪些边界'],
    crystal: n
      ? [`整理一下和「${n}」最近的待办与礼数`, `最近记了哪些关于「${n}」的要点？`]
      : ['整理一下最近的待办与礼数', '这段时间记过哪些要点？'],
  };
  return (byTier[tier] || byTier.crystal).map((prompt, idx) =>
    h('button', { type: 'button', class: 'cupid-starter-chip', style: { '--stagger': `${idx * 48}ms` }, onClick: () => (haptic('light'), onPick(prompt)) },
      h('span', { class: 'mw-dot', 'aria-hidden': 'true' }),
      h('span', { text: prompt }),
    ),
  );
}

/** Right-edge drawer card (strictly 0 <img> on Silver tier). */
export function drawerCard(m, { selected, tier, onToggle, onPeek }) {
  const snippet = cleanMaterialLabel(m, 64);
  const date = formatDateCn(m.happenedOn || m.createdAt);
  const dateMeta = m.type === 'voice' && m.dur ? [date, m.dur].filter(Boolean).join(' · ') : date || (m.type === 'voice' ? '语音' : '手帐');
  const showPhoto = m.type === 'photo' && m.mediaUrl && tier !== 'silver';
  const kindIcon = showPhoto ? null : kindSvg(m.type === 'voice' ? 'voice' : 'note');
  if (kindIcon?.style) { kindIcon.style.width = '16px'; kindIcon.style.height = '16px'; kindIcon.style.flexShrink = '0'; }
  return h('button', {
    type: 'button', class: ['drawer-mat-card', selected && 'is-selected'], dataset: { materialId: m.id }, 'aria-pressed': String(Boolean(selected)),
    onClick: onToggle, onContextmenu: (e) => onPeek && (e.preventDefault(), onPeek()),
  },
    showPhoto
      ? h('img', { class: 'drawer-mat-thumb', src: m.mediaUrl, alt: '' })
      : h('span', { class: 'drawer-mat-icon-box', dataset: { kind: m.type === 'voice' ? 'voice' : 'note' }, 'aria-hidden': 'true' }, kindIcon),
    h('span', { class: 'drawer-mat-meta' }, h('span', { class: 'drawer-mat-snippet', text: snippet }), h('span', { class: 'drawer-mat-date', text: dateMeta })),
    h('span', { class: 'drawer-mat-check', 'aria-hidden': 'true', text: '✓' }),
  );
}

export function attachedChip(m, onRemove) {
  const chipIcon = kindSvg(m.type === 'voice' ? 'voice' : 'note');
  if (chipIcon?.style) { chipIcon.style.width = '13px'; chipIcon.style.height = '13px'; chipIcon.style.flexShrink = '0'; }
  return h('span', { class: 'attached-chip' },
    h('span', { class: 'attached-chip-badge', 'aria-hidden': 'true' }, chipIcon),
    h('span', { class: 'attached-chip-title', text: cleanMaterialLabel(m, 16) }),
    h('button', { type: 'button', class: 'attached-chip-remove', 'aria-label': '不附这页了', text: '✕', onClick: () => (haptic('light'), onRemove()) }),
  );
}
