// peek-date.js — Zero-blur Slip Peek Overlay (mirroring 100% of native UIMenu actions),
// native UIAlertController destructive delete + 5.0s Undo, collapsed 原话 + BC5 audio,
// and Pure Chinese 3-Column Scroll-Snap Porcelain Date Picker (0 <input type="date">, CoreHaptics porcelainTick!).
import { h, clear, $ } from '../dom.js';
import { api } from '../api/endpoints.js';
import { store, patchMaterial, removeMaterial, upsertMaterial } from '../store.js';
import { formatDateCn, formatDateFullCn, todayIso } from '../format.js';
import { openSheet, closeSheet } from './sheet.js';
import { showUndoToast, whisper } from './toast.js';
import { actions } from '../actions.js';
import { haptic, hapticPrepare, copyText, mwNativeUI, sharePayload } from '../native-bridge.js';
import { createImeGuard } from '../lib/ime-guard.js';
import { formatLunarDateCn } from '../lib/solar-terms.js';
import { renderAudioPill, stopActiveAudio } from './journal-items.js';

const CLOSE_MS = 220;
const ROW_H = 40;
let activePeek = null;

function daysInMonth(year, month) {
  return new Date(year, month, 0).getDate();
}

function parseIsoParts(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso || ''));
  if (m) return { y: Number(m[1]), m: Number(m[2]), d: Number(m[3]) };
  const now = new Date();
  return { y: now.getFullYear(), m: now.getMonth() + 1, d: now.getDate() };
}

function toIso({ y, m, d }) {
  return `${y}-${String(m).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
}

function clampDate(parts, mode) {
  const today = parseIsoParts(todayIso());
  const clampedDay = Math.min(parts.d, daysInMonth(parts.y, parts.m));
  const candidate = { y: parts.y, m: parts.m, d: clampedDay };
  const candIso = toIso(candidate);
  const todIso = toIso(today);
  if (mode === 'past' && candIso > todIso) return today;
  if (mode === 'future' && candIso < todIso) return today;
  return candidate;
}

/**
 * Pure Chinese 3-Column Scroll-Snap Porcelain Date Picker (ZERO <input type="date">!).
 * mode: 'past' (happened_on <= today) | 'future' (trigger_date >= today) | 'any'
 * Fires CoreHaptics porcelainTick (throttled >= 45ms) on every snapped wheel row detent!
 */
export function openChineseDatePicker({ initialIso = todayIso(), mode = 'past', title = '选择日期', onConfirm } = {}) {
  const today = parseIsoParts(todayIso());
  let sel = clampDate(parseIsoParts(initialIso), mode);
  const minYear = mode === 'future' ? today.y : Math.max(1950, today.y - 60);
  const maxYear = mode === 'past' ? today.y : today.y + 10;
  const years = [];
  for (let y = minYear; y <= maxYear; y++) years.push(y);
  const months = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12];

  let lastTickAt = 0;
  const tickHaptic = () => {
    const now = performance.now();
    if (now - lastTickAt >= 45) {
      lastTickAt = now;
      haptic('porcelainTick');
    }
  };

  const previewEl = h('p', { class: 'date-wheel-preview' });
  const updatePreview = () => {
    const dateObj = new Date(sel.y, sel.m - 1, sel.d);
    const lunar = formatLunarDateCn ? formatLunarDateCn(dateObj) : '';
    previewEl.textContent = lunar
      ? `${sel.y}年${sel.m}月${sel.d}日 · ${lunar}`
      : `${sel.y}年${sel.m}月${sel.d}日`;
  };

  function makeColumn(values, formatFn, getVal, setVal, ariaLabel) {
    const col = h('div', { class: 'date-wheel-col', role: 'listbox', 'aria-label': ariaLabel });
    let lastIdx = -1;
    let scrollTimer = 0;

    const rebuild = (list) => {
      clear(col);
      col.appendChild(h('div', { class: 'date-wheel-pad', 'aria-hidden': 'true' }));
      for (const v of list) {
        col.appendChild(h('button', {
          type: 'button',
          role: 'option',
          class: ['date-wheel-item', v === getVal() && 'is-selected'],
          dataset: { val: String(v) },
          text: formatFn(v),
          onClick: () => {
            const idx = list.indexOf(v);
            col.scrollTo({ top: idx * ROW_H, behavior: 'smooth' });
            setVal(v);
            tickHaptic();
          },
        }));
      }
      col.appendChild(h('div', { class: 'date-wheel-pad', 'aria-hidden': 'true' }));
      const idx = Math.max(0, list.indexOf(getVal()));
      lastIdx = idx;
      requestAnimationFrame(() => { col.scrollTop = idx * ROW_H; });
    };

    col.addEventListener('pointerdown', () => hapticPrepare('porcelainTick'), { passive: true });
    col.addEventListener('scroll', () => {
      const items = Array.from(col.querySelectorAll('.date-wheel-item'));
      if (!items.length) return;
      const idx = Math.max(0, Math.min(items.length - 1, Math.round(col.scrollTop / ROW_H)));
      if (idx !== lastIdx) {
        lastIdx = idx;
        tickHaptic();
        items.forEach((n, i) => n.classList.toggle('is-selected', i === idx));
        const val = Number(items[idx].dataset.val);
        clearTimeout(scrollTimer);
        scrollTimer = setTimeout(() => setVal(val), 80);
      }
    }, { passive: true });

    rebuild(values);
    return { el: col, rebuild };
  }

  let dayCol;
  const syncAfterChange = () => {
    sel = clampDate(sel, mode);
    const maxD = daysInMonth(sel.y, sel.m);
    const days = [];
    for (let d = 1; d <= maxD; d++) days.push(d);
    dayCol?.rebuild(days);
    updatePreview();
  };

  const yearCol = makeColumn(years, (y) => `${y}年`, () => sel.y, (y) => { sel.y = y; syncAfterChange(); }, '年份');
  const monthCol = makeColumn(months, (m) => `${m}月`, () => sel.m, (m) => { sel.m = m; syncAfterChange(); }, '月份');
  const initDays = Array.from({ length: daysInMonth(sel.y, sel.m) }, (_, i) => i + 1);
  dayCol = makeColumn(initDays, (d) => `${d}日`, () => sel.d, (d) => { sel.d = d; sel = clampDate(sel, mode); updatePreview(); }, '日期');

  updatePreview();
  const wheel = h(
    'div',
    { class: 'date-wheel-stage' },
    h('div', { class: 'date-wheel-band', 'aria-hidden': 'true' }),
    yearCol.el,
    monthCol.el,
    dayCol.el,
  );

  const actionsRow = h(
    'div',
    { class: 'sheet-actions' },
    h('button', { type: 'button', class: 'sheet-btn quiet', text: '取消', onClick: () => closeSheet() }),
    h('button', {
      type: 'button',
      class: 'sheet-btn',
      text: '确定',
      onClick: () => {
        const finalIso = toIso(clampDate(sel, mode));
        haptic('selection');
        closeSheet({ silentHaptic: true });
        onConfirm?.(finalIso);
      },
    }),
  );

  return openSheet({
    title,
    sub: mode === 'past' ? '可选择今天或更早的日期' : mode === 'future' ? '可选择今天或之后的提醒日期' : '选择公历日期',
    body: [previewEl, wheel, actionsRow],
    className: 'is-date-picker-sheet',
    stacked: true,
  });
}

function syncPeekOverlayState() {
  const hasAny = Boolean(
    activePeek ||
    $('#sheetLayer')?.classList.contains('is-open') ||
    $('#chat')?.classList.contains('is-active'),
  );
  $('#app')?.classList.toggle('has-overlay', hasAny);
  for (const id of ['journalCanvas', 'dock']) {
    const el = $(`#${id}`);
    if (el) el.inert = hasAny;
  }
}

export function closePeek({ silentHaptic = false } = {}) {
  if (!activePeek) return;
  const entry = activePeek;
  activePeek = null;
  if (entry.rafId) cancelAnimationFrame(entry.rafId);
  if (entry.focusTimerId) clearTimeout(entry.focusTimerId);
  if (entry.clearTimerId) clearTimeout(entry.clearTimerId);
  entry.ac.abort();
  entry.imeGuard?.destroy?.();
  stopActiveAudio();
  if (!silentHaptic) haptic('light');
  entry.layer.classList.remove('is-open');
  syncPeekOverlayState();

  entry.clearTimerId = setTimeout(() => {
    if (!activePeek) {
      entry.layer.style.display = 'none';
      clear(entry.layer);
    }
  }, CLOSE_MS + 20);

  const r = entry.restore;
  if (r && typeof r.focus === 'function' && r.isConnected) r.focus({ preventScroll: true });
}

export function openPeekOverlay(materialId, person, { startEditing = false, onMutated } = {}) {
  const layer = $('#peekLayer');
  const fresh = () => store.get().materials[person?.id]?.items?.find((x) => x.id === materialId) || null;
  const m = fresh();
  if (!layer || !person || !m) return;
  if (activePeek) closePeek({ silentHaptic: true });
  clear(layer);
  layer.style.display = 'flex';
  haptic('light');

  const ac = new AbortController();
  const tier = person.tier || 'crystal';
  const isSilver = tier === 'silver';
  const showPhoto = !isSilver && m.type === 'photo' && Boolean(m.mediaUrl);
  let editing = false;
  let editDate = m.happenedOn || todayIso();

  const dateTextEl = h('p', { class: 'peek-date', text: formatDateFullCn(m.happenedOn || m.createdAt) || formatDateCn(m.happenedOn || m.createdAt) });
  const bodyHost = h('div', { class: 'peek-body-host' }, h('p', { class: 'peek-body', text: m.body || m.snippet || '' }));

  // P0-06: Dedicated Confirm / Ink banner for pencil drafts so users can settle 待确认 slips directly inside Peek
  let pencilBanner = null;
  if (m.presentation === 'pencil') {
    const inkBtn = h('button', {
      type: 'button',
      class: 'peek-pencil-ink-btn',
      text: '✓ 确认落墨',
      onClick: async () => {
        inkBtn.disabled = true;
        inkBtn.textContent = '落墨中…';
        try {
          const latest = fresh() || m;
          const tasks = [];
          if (latest.inboxItemId && typeof api.resolveInbox === 'function') {
            tasks.push(api.resolveInbox(latest.inboxItemId, { action: 'confirm' }));
          }
          if (Array.isArray(latest.pencilFacts) && latest.pencilFacts.length) {
            for (const f of latest.pencilFacts) {
              if (f?.id) tasks.push(api.patchFact(f.id, { status: 'confirmed' }, f.version));
            }
          }
          if (!tasks.length) {
            tasks.push(api.patchMaterial(latest.id, { reviewState: 'normal' }, latest.version));
          }
          await Promise.all(tasks);
          patchMaterial(person.id, latest.id, { presentation: 'ink', pencilFacts: [], inboxItemId: null });
          card.classList.remove('is-pencil');
          pencilBanner?.remove();
          haptic('memorySeal', { tier });
          actions.refreshBootstrap?.();
          onMutated?.();
          whisper('好，已经核对落墨', 2000);
        } catch (err) {
          inkBtn.disabled = false;
          inkBtn.textContent = '✓ 确认落墨';
          haptic('warning');
          whisper(err?.message || '暂时没能落墨，请再试一次', 2600);
        }
      },
    });
    pencilBanner = h('div', { class: 'peek-pencil-banner', role: 'region', 'aria-label': '待确认核对' },
      h('span', { class: 'peek-pencil-copy', text: m.glanceHint || '待确认 · 核对无误后即可落墨' }),
      inkBtn,
    );
  }

  const rawText = m.transcript || m.rawTranscript || m.rawText || '';
  const rawDetails = rawText && rawText !== m.body
    ? h('details', { class: 'peek-raw-details' }, h('summary', { text: '原话' }), h('p', { class: 'peek-raw-text', text: rawText }))
    : null;

  const card = h(
    'div',
    { class: ['peek-card', `tier-${tier}`, m.presentation === 'pencil' && 'is-pencil'], role: 'dialog', 'aria-modal': 'true', tabindex: '-1', 'aria-label': '细看这条手帐' },
    h('div', { class: 'peek-grabber-hit', 'aria-hidden': 'true' }, h('div', { class: 'peek-grabber' })),
    pencilBanner,
    showPhoto
      ? h('div', {
          class: 'peek-photo-wrap',
          onClick: () => mwNativeUI?.previewMedia?.({ url: m.mediaUrl, title: `${formatDateCn(m.happenedOn || m.createdAt)} · ${person.nameCn}`, tier }),
        }, h('img', { src: m.mediaUrl, alt: '手帐照片' }))
      : null,
    (m.type === 'voice' || m.audioUrl) ? h('div', { class: 'peek-voice-row' }, renderAudioPill(m)) : null,
    bodyHost,
    rawDetails,
    dateTextEl,
  );

  const entry = { layer, ac, restore: document.activeElement, rafId: 0, focusTimerId: 0, clearTimerId: 0, imeGuard: null };
  let pill = null;

  const exitEdit = () => {
    editing = false;
    entry.imeGuard?.destroy?.();
    entry.imeGuard = null;
    const cur = fresh() || m;
    clear(bodyHost);
    bodyHost.appendChild(h('p', { class: 'peek-body', text: cur.body || cur.snippet || '' }));
    dateTextEl.textContent = formatDateFullCn(cur.happenedOn || cur.createdAt) || formatDateCn(cur.happenedOn || cur.createdAt);
    dateTextEl.hidden = false;
    if (pill) pill.hidden = false;
  };

  const enterEdit = () => {
    if (editing) return exitEdit();
    editing = true;
    haptic('light');
    dateTextEl.hidden = true;
    if (pill) pill.hidden = true;
    const cur = fresh() || m;
    editDate = cur.happenedOn || todayIso();
    const ta = h('textarea', { class: 'peek-edit-area', rows: '4', maxlength: '10000', value: cur.body || cur.snippet || '', 'aria-label': '修改记录文字' });
    const dateBtn = h('button', {
      type: 'button',
      class: 'peek-date-chip',
      text: `${formatDateFullCn(editDate)} ▾`,
      onClick: () => {
        openChineseDatePicker({
          initialIso: editDate,
          mode: 'past',
          title: '选择发生日期',
          onConfirm: (iso) => {
            editDate = iso;
            dateBtn.textContent = `${formatDateFullCn(iso)} ▾`;
          },
        });
      },
    });
    const saveBtn = h('button', { type: 'button', class: 'peek-confirm-btn', text: '保存修改' });
    const doSave = async () => {
      const nextText = ta.value.trim();
      if (!nextText) return whisper('写点什么再保存吧', 2000);
      saveBtn.disabled = true;
      try {
        const latest = fresh() || cur;
        const patch = { body: nextText, snippet: nextText.slice(0, 60), happenedOn: editDate };
        const updated = await api.patchMaterial(latest.id, patch, latest.version);
        patchMaterial(person.id, latest.id, updated || patch);
        haptic('memorySeal', { tier });
        onMutated?.();
        exitEdit();
        whisper('已保存修改', 2000);
      } catch (err) {
        saveBtn.disabled = false;
        haptic('warning');
        whisper(err?.message || '未能保存修改', 2800);
      }
    };
    saveBtn.addEventListener('click', doSave);
    entry.imeGuard = createImeGuard?.(ta, { onSubmit: doSave, isTouchDevice: true }) || null;
    clear(bodyHost);
    bodyHost.append(
      ta,
      h('div', { class: 'peek-edit-bar' }, dateBtn, h('button', { type: 'button', class: 'glance-btn quiet', text: '取消', onClick: exitEdit }), saveBtn),
    );
    requestAnimationFrame(() => ta.focus({ preventScroll: true }));
  };

  const confirmDelete = async () => {
    const res = await mwNativeUI?.showSheet?.({
      style: 'actionSheet',
      title: '删除这条手帐？',
      message: '删除后可在 5 秒内撤销。',
      items: [
        { id: 'delete', label: '删除手帐', title: '删除手帐', role: 'destructive', destructive: true },
        { id: 'cancel', label: '取消', title: '取消', role: 'cancel', cancel: true },
      ],
    });
    if (res && res.selectedId !== 'delete') return;
    const backup = fresh() || m;
    removeMaterial(person.id, backup.id);
    closePeek({ silentHaptic: true });
    haptic('warning');
    onMutated?.();
    let undone = false;
    showUndoToast('已删除这条手帐', async () => {
      undone = true;
      upsertMaterial(person.id, backup);
      onMutated?.();
      whisper('已恢复这条手帐', 2000);
    }, {
      onExpire: async () => {
        if (undone) return;
        try { await api.deleteMaterial(backup.id); } catch { /* ignore */ }
      },
    });
  };

  const actionsList = [
    h('button', {
      type: 'button',
      class: 'peek-pill-btn',
      text: m.pinned ? '取消置顶' : '置顶',
      onClick: async () => {
        const latest = fresh() || m;
        const nextPinned = !latest.pinned;
        try {
          const updated = await api.patchMaterial(latest.id, { pinned: nextPinned }, latest.version);
          patchMaterial(person.id, latest.id, updated || { pinned: nextPinned });
          haptic('selection');
          closePeek({ silentHaptic: true });
          whisper(nextPinned ? '已置顶这条记录' : '已取消置顶', 1800);
        } catch (err) { whisper(err?.message || '操作失败', 2400); }
      },
    }),
    h('button', { type: 'button', class: 'peek-pill-btn', text: '编辑', onClick: enterEdit }),
    h('button', {
      type: 'button',
      class: 'peek-pill-btn',
      text: '换人',
      onClick: () => {
        closePeek({ silentHaptic: true });
        actions.openPeopleSheet?.({
          title: '换给哪一位人物？',
          onSelect: async (target) => {
            if (!target || target.id === person.id) return;
            try {
              const latest = fresh() || m;
              await api.patchMaterial(latest.id, { personId: target.id }, latest.version);
              removeMaterial(person.id, latest.id);
              actions.reloadMaterials?.(target.id);
              haptic('memorySeal', { tier: target.tier });
              whisper(`已移至「${target.nameCn}」的手帐`, 2400);
            } catch (err) { whisper(err?.message || '换人失败', 2600); }
          },
        });
      },
    }),
    h('button', {
      type: 'button',
      class: 'peek-pill-btn',
      text: '复制',
      onClick: async () => {
        const txt = (fresh() || m).body || '';
        if (isSilver && mwNativeUI?.copySensitive) await mwNativeUI.copySensitive(txt);
        else await copyText(txt);
        haptic('light');
        whisper(isSilver ? '已仅限本机复制（5 分钟后自动清除）' : '已复制文字', 2200);
      },
    }),
    h('button', {
      type: 'button',
      class: 'peek-pill-btn',
      text: '问丘比特',
      onClick: () => { haptic('light'); closePeek({ silentHaptic: true }); actions.openChat?.(person.id, { materialId: m.id }); },
    }),
    !isSilver
      ? h('button', {
          type: 'button',
          class: 'peek-pill-btn',
          text: '分享',
          onClick: async () => {
            await sharePayload?.({ title: `${person.nameCn} · 手帐`, text: (fresh() || m).body || '' });
          },
        })
      : null,
    h('button', { type: 'button', class: 'peek-pill-btn is-danger', text: '删除', onClick: confirmDelete }),
  ];

  pill = h('div', { class: 'peek-action-pill', role: 'toolbar', 'aria-label': '手帐操作' }, ...actionsList);
  const scrim = h('div', { class: 'peek-scrim', onClick: () => closePeek() });
  const stage = h('div', { class: 'peek-stage', onClick: (e) => { if (e.target === stage) closePeek(); } }, card, pill);
  layer.append(scrim, stage);

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.preventDefault(); editing ? exitEdit() : closePeek(); }
  }, { signal: ac.signal });

  activePeek = entry;
  syncPeekOverlayState();
  entry.rafId = requestAnimationFrame(() => {
    entry.rafId = 0;
    if (activePeek !== entry) return;
    layer.classList.add('is-open');
    if (startEditing) enterEdit();
    else entry.focusTimerId = setTimeout(() => { if (activePeek === entry) card.focus({ preventScroll: true }); }, 90);
  });
}

export { openPeekOverlay as openPeek };
