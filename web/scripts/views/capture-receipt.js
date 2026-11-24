// capture-receipt.js — 0–3s Single Expandable Porcelain Routing Receipt Pill & Tray (0 blur).
// Reuses static #captureReceipt from index.html (0 duplicate IDs), aligned 100% with BC6
// CaptureResponse DTO (transcript, top-level candidatePersons, split.tier, split.tag).
import { h, clear, $ } from '../dom.js';
import { api } from '../api/endpoints.js';
import { store, removeMaterial } from '../store.js';
import { monogramOf, sealStyle } from '../decor.js';
import { namesList, clip } from '../format.js';
import { whisper } from './toast.js';
import { actions } from '../actions.js';
import { haptic } from '../native-bridge.js';

const AUTO_DISMISS_MS = 10_000;
let activeReceipt = null;

export function dismissCaptureReceipt() {
  if (!activeReceipt) return;
  const cur = activeReceipt;
  activeReceipt = null;
  clearTimeout(cur.timer);
  cur.ac.abort();
  cur.el.classList.remove('is-open');
  setTimeout(() => {
    if (!activeReceipt) {
      cur.el.style.display = 'none';
      clear(cur.el);
    }
  }, 220);
}

function renderSplitRow(split, topCandidates, { onMutated }) {
  const person = store.get().people[split.personId] || { id: split.personId, nameCn: split.personName || '未命名', tier: split.tier || split.personTier || 'crystal' };
  const tier = person.tier || split.tier || split.personTier || 'crystal';
  const silver = tier === 'silver';
  const usePhoto = !silver && Boolean(person.avatarUrl);

  const token = h('span', { class: ['receipt-avatar', silver && 'is-cloaked'], dataset: { tier }, style: usePhoto || silver ? {} : sealStyle(person) });
  if (usePhoto) token.appendChild(h('img', { src: person.avatarUrl, alt: '' }));
  else token.textContent = monogramOf(person);

  const nameEl = h('strong', { class: 'receipt-person-name', text: person.nameCn });
  const bodyEl = h('p', { class: 'receipt-split-body', text: clip(split.body || split.snippet || split.title || split.tag || '', 72) });

  const reassignBtn = split.materialId
    ? h('button', {
        type: 'button',
        class: 'receipt-mini-btn',
        text: '换人',
        onClick: () => actions.openPeopleSheet?.({
          title: '这条归档换给哪一位？',
          onSelect: async (target) => {
            if (!target || target.id === split.personId) return;
            try {
              await api.patchMaterial(split.materialId, { personId: target.id }, split.version);
              removeMaterial(split.personId, split.materialId);
              split.personId = target.id;
              split.personName = target.nameCn;
              split.tier = target.tier;
              nameEl.textContent = target.nameCn;
              actions.reloadMaterials?.(target.id);
              haptic('selection');
              onMutated?.();
              whisper(`已改记到「${target.nameCn}」`, 2200);
            } catch (err) { whisper(err?.message || '换人失败', 2400); }
          },
        }),
      })
    : null;

  const extra = [];
  if (split.selfCorrectedFrom) {
    const restoreBtn = h('button', {
      type: 'button',
      class: 'capture-receipt-btn receipt-restore-link',
      text: `恢复「${clip(split.selfCorrectedFrom, 14)}」`,
      onClick: async () => {
        if (!split.materialId) return;
        restoreBtn.disabled = true;
        try {
          await api.patchMaterial(split.materialId, { body: split.selfCorrectedFrom, snippet: split.selfCorrectedFrom.slice(0, 60) }, split.version);
          bodyEl.textContent = clip(split.selfCorrectedFrom, 72);
          haptic('selection');
          actions.reloadMaterials?.(split.personId);
          whisper('已恢复为原句', 2000);
        } catch (err) { restoreBtn.disabled = false; whisper(err?.message || '未能恢复', 2200); }
      },
    });
    extra.push(h('div', { class: 'capture-receipt-chips receipt-correction-row' }, h('span', { text: '已自动润色更正 · ' }), restoreBtn));
  }

  // BC6: candidatePersons lives at top-level CaptureResponse (or defensively on split)
  const candidates = (split.candidatePersons?.length ? split.candidatePersons : topCandidates || []).filter((c) => c?.personId).slice(0, 3);
  if (candidates.length && split.inboxItemId) {
    const candRow = h(
      'div',
      { class: 'capture-receipt-chips receipt-referclaw-row' },
      h('span', { class: 'receipt-referclaw-label', text: '指的是哪一位？' }),
      ...candidates.map((c) =>
        h('button', {
          type: 'button',
          class: 'capture-receipt-btn receipt-cand-chip',
          text: c.distinguishingCue ? `${c.displayName}（${c.distinguishingCue}）` : c.displayName,
          onClick: async () => {
            try {
              await api.resolveInbox(split.inboxItemId, { action: 'assign_person', targetPersonId: c.personId });
              removeMaterial(split.personId, split.materialId);
              actions.reloadMaterials?.(c.personId);
              actions.refreshBootstrap?.();
              candRow.remove();
              nameEl.textContent = c.displayName;
              haptic('memorySeal', { tier: c.tier });
              whisper(`已归到「${c.displayName}」名下`, 2200);
            } catch (err) { whisper(err?.message || '确认失败', 2400); }
          },
        }),
      ),
    );
    extra.push(candRow);
  }

  return h(
    'div',
    { class: 'capture-receipt-split receipt-split-row' },
    h('div', { class: 'capture-receipt-split-head receipt-split-head' }, h('span', { style: { display: 'inline-flex', 'align-items': 'center', gap: '8px', 'min-width': '0' } }, token, nameEl), reassignBtn),
    bodyEl,
    ...extra,
  );
}

export function openCaptureReceipt(res, { captureId = null, onUndo = null } = {}) {
  if (!res) return;
  dismissCaptureReceipt();

  let el = $('#captureReceipt');
  if (!el) {
    el = h('div', { class: 'capture-receipt-pill', id: 'captureReceipt', role: 'status', 'aria-live': 'polite' });
    ($('#app') || document.body).appendChild(el);
  }
  clear(el);

  const splits = res.splits || [];
  const names = [...new Set(splits.map((s) => s.personName || store.get().people[s.personId]?.nameCn).filter(Boolean))];
  const primaryTier = splits[0]?.tier || splits[0]?.personTier || store.get().people[splits[0]?.personId]?.tier || 'gold';
  const cid = captureId || res.captureId || res.jobId || null;

  haptic('memorySeal', { tier: primaryTier });

  const ac = new AbortController();
  let expanded = false;
  let paused = false;
  let remaining = AUTO_DISMISS_MS;
  let startedAt = Date.now();

  const summaryText = names.length ? `已分记至${namesList(names)}（${splits.length} 条）` : splits.length ? `已整理 ${splits.length} 条记录` : '这句话已暂存在最近归档';
  const trayHost = h('div', { class: 'capture-receipt-tray', style: { display: 'none' } });
  const expandLabel = h('span', { class: 'capture-receipt-btn receipt-pill-toggle', text: '展开 ▾' });

  const undoDeadlineMs = res.undoDeadline ? new Date(res.undoDeadline).getTime() : Date.now() + 30_000;
  const canUndo = Boolean(cid && undoDeadlineMs > Date.now() + 1000 && onUndo);
  const undoBtn = canUndo
    ? h('button', {
        type: 'button',
        class: 'receipt-undo-btn',
        text: '撤回',
        onClick: async (e) => { e.stopPropagation(); undoBtn.disabled = true; dismissCaptureReceipt(); await onUndo?.(); },
      })
    : null;

  const pillHead = h(
    'div',
    { class: 'capture-receipt-head', role: 'button', tabindex: '0', 'aria-expanded': 'false', onClick: () => toggleExpand() },
    h('span', { class: 'mw-dot is-warm', 'aria-hidden': 'true' }),
    h('span', { class: 'capture-receipt-summary receipt-pill-text', text: summaryText }),
    h('span', { class: 'capture-receipt-actions' }, undoBtn, expandLabel),
  );
  el.append(pillHead, trayHost);

  const pauseTimer = () => {
    if (paused) return;
    paused = true;
    remaining = Math.max(1500, remaining - (Date.now() - startedAt));
    clearTimeout(entry.timer);
  };
  const resumeTimer = () => {
    if (!paused || expanded || activeReceipt !== entry) return;
    paused = false;
    startedAt = Date.now();
    entry.timer = setTimeout(dismissCaptureReceipt, remaining);
  };

  const buildTray = () => {
    clear(trayHost);
    const raw = res.transcript || res.rawTranscript || res.repairedTranscript || '';
    if (raw) {
      trayHost.appendChild(h('details', { class: 'peek-verbatim-box receipt-raw-details' }, h('summary', { text: '原话' }), h('p', { class: 'receipt-raw-text', text: raw })));
    }
    for (const s of splits) trayHost.appendChild(renderSplitRow(s, res.candidatePersons, { onMutated: pauseTimer }));
    trayHost.appendChild(
      h(
        'div',
        { class: 'capture-receipt-foot receipt-tray-foot' },
        h('button', { type: 'button', class: 'receipt-history-link', text: '全部最近归档 ›', onClick: () => { dismissCaptureReceipt(); actions.openArchiveHistory?.(); } }),
        h('button', { type: 'button', class: 'capture-receipt-btn receipt-close-link', text: '收起', onClick: dismissCaptureReceipt }),
      ),
    );
  };

  const toggleExpand = () => {
    expanded = !expanded;
    haptic('light');
    pillHead.setAttribute('aria-expanded', String(expanded));
    expandLabel.textContent = expanded ? '收起 ▴' : '展开 ▾';
    el.classList.toggle('is-expanded', expanded);
    if (expanded) { pauseTimer(); buildTray(); trayHost.style.display = 'flex'; }
    else { trayHost.style.display = 'none'; resumeTimer(); }
  };

  el.addEventListener('touchstart', pauseTimer, { passive: true, signal: ac.signal });
  el.addEventListener('pointerenter', pauseTimer, { signal: ac.signal });
  el.addEventListener('pointerleave', resumeTimer, { signal: ac.signal });

  const entry = { el, ac, timer: setTimeout(dismissCaptureReceipt, AUTO_DISMISS_MS), res, captureId: cid };
  activeReceipt = entry;
  // Stack pill header & expanded tray vertically so header NEVER crushes into a 1-char column!
  el.style.display = 'flex';
  el.style.flexDirection = 'column';
  requestAnimationFrame(() => el.classList.add('is-open'));
}

export { openCaptureReceipt as showCaptureReceipt };
