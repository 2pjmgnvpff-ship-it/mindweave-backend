// archive-history-sheet.js — User Lock C1「最近归档记录」full-CRUD management sheet (0 blur).
// Replaces retired questions.js. Merges local IDB outbox at top + cursor-paged GET /v1/captures
// aligned 100% with BC6 live FastAPI DTOs (captureId, durationMs, transcript, source='voice',
// POST /v1/captures/{captureId}/slips for '+ 从这段话补拆一条', PATCH personId, 2-tap/showSheet delete).
import { h, clear } from '../dom.js';
import { api, client } from '../api/endpoints.js';
import { store, removeMaterial, patchMaterial, upsertMaterial } from '../store.js';
import { formatDateCn, formatDateFullCn, todayIso, clip } from '../format.js';
import { openSheet } from './sheet.js';
import { showUndoToast, whisper } from './toast.js';
import { actions } from '../actions.js';
import { haptic, copyText, mwNativeUI } from '../native-bridge.js';
import { createImeGuard } from '../lib/ime-guard.js';
import { renderAudioPill, stopActiveAudio } from './journal-items.js';
import { openChineseDatePicker } from './peek-date.js';
import { draftQueue, flushDrafts, undoCapture } from './capture.js';

const FILTERS = [
  { id: 'all', label: '全部' },
  { id: 'pending', label: '待确认' },
  { id: 'voice', label: '语音' },
];

function createCaptureSlipRequest(captureId, payload) {
  if (typeof api.createCaptureSlip === 'function') return api.createCaptureSlip(captureId, payload);
  if (typeof api.addCaptureSlip === 'function') return api.addCaptureSlip(captureId, payload);
  return client.post(`/captures/${encodeURIComponent(captureId)}/slips`, payload);
}

function listCapturesRequest({ cursor, limit = 20, filter = 'all' }) {
  if (typeof api.listCaptures === 'function') return api.listCaptures({ cursor, limit, filter });
  const query = { limit, cursor: cursor || undefined };
  if (filter === 'voice') query.source = 'voice';
  return client.get('/captures', { query });
}

function renderLocalOutboxSection(drafts, onChanged) {
  if (!drafts.length) return null;
  const wrap = h('section', { class: 'archive-outbox-section', 'aria-label': '本机暂存草稿' });
  wrap.appendChild(h('h3', { class: 'archive-sec-title', text: `本机离线暂存（${drafts.length} 条）` }));
  for (const d of drafts) {
    const quarantined = d.status === 'quarantined';
    const text = d.clientTranscript || d.text || '一段待上传的语音记录';
    const card = h(
      'div',
      { class: ['archive-run-card', 'is-local', quarantined && 'is-quarantined'] },
      h('div', { class: 'archive-run-head' }, h('span', { class: 'archive-source-tag', text: quarantined ? '未能自动入册' : '等待联网补传' }), h('span', { class: 'archive-time', text: formatDateCn(d.recordedAt || d.createdAt) })),
      h('p', { class: 'archive-split-text', text: clip(text, 120) }),
      h(
        'div',
        { class: 'archive-crud-row' },
        h('button', {
          type: 'button',
          class: 'archive-mini-btn',
          text: '立即重试',
          onClick: async () => {
            haptic('light');
            if (quarantined) await Promise.resolve((draftQueue.retryDraft || draftQueue.retryQuarantined)?.(d.id)).catch(() => {});
            await flushDrafts();
            onChanged();
          },
        }),
        text
          ? h('button', {
              type: 'button',
              class: 'archive-mini-btn',
              text: '复制文字',
              onClick: async () => { await copyText(text); haptic('light'); whisper('已复制草稿文字', 1800); },
            })
          : null,
        h('button', {
          type: 'button',
          class: 'archive-mini-btn is-danger',
          text: '舍弃草稿',
          onClick: async () => {
            const res = await mwNativeUI?.showSheet?.({
              style: 'actionSheet',
              title: '舍弃这条本机草稿？',
              items: [
                { id: 'drop', label: '舍弃草稿', title: '舍弃草稿', role: 'destructive', destructive: true },
                { id: 'cancel', label: '取消', title: '取消', role: 'cancel', cancel: true },
              ],
            });
            if (res && res.selectedId !== 'drop') return;
            await Promise.resolve((draftQueue.discardDraft || draftQueue.remove)?.(d.id)).catch(() => {});
            haptic('warning');
            onChanged();
          },
        }),
      ),
    );
    wrap.appendChild(card);
  }
  return wrap;
}

function renderSplitItem(split, run, { onRefresh }) {
  const person = store.get().people[split.personId] || { id: split.personId, nameCn: split.personName || '未命名', tier: split.tier || 'crystal' };
  const tier = person.tier || split.tier || 'crystal';
  const bodyHost = h('div', { class: 'archive-split-body-host' });
  const personChip = h('strong', { class: ['archive-person-tag', `tier-${tier}`], text: person.nameCn });
  const dateChip = h('span', { class: 'archive-split-date', text: formatDateCn(split.happenedOn || run.createdAt) });
  const row = h('div', { class: 'archive-split-item' });

  let editing = false;
  let editDate = split.happenedOn || todayIso();
  let imeGuard = null;

  const renderRead = () => {
    editing = false;
    imeGuard?.destroy?.();
    imeGuard = null;
    clear(bodyHost);
    bodyHost.appendChild(h('p', { class: 'archive-split-text', text: split.body || split.snippet || '' }));
    dateChip.textContent = formatDateCn(split.happenedOn || run.createdAt);
  };

  const enterEdit = () => {
    if (editing) return renderRead();
    editing = true;
    editDate = split.happenedOn || todayIso();
    const ta = h('textarea', { class: 'mw-field-textarea archive-edit-input', rows: '3', maxlength: '4000', value: split.body || '', 'aria-label': '修改记录正文' });
    const dateBtn = h('button', {
      type: 'button',
      class: 'archive-mini-btn',
      text: `${formatDateFullCn(editDate)} ▾`,
      onClick: () => openChineseDatePicker({
        initialIso: editDate,
        mode: 'past',
        title: '修改发生日期',
        onConfirm: (iso) => { editDate = iso; dateBtn.textContent = `${formatDateFullCn(iso)} ▾`; },
      }),
    });
    const saveBtn = h('button', { type: 'button', class: 'sheet-btn archive-save-btn', text: '保存' });
    const doSave = async () => {
      const next = ta.value.trim();
      if (!next) return;
      saveBtn.disabled = true;
      const prevBody = split.body;
      const prevDate = split.happenedOn;
      split.body = next;
      split.happenedOn = editDate;
      patchMaterial(split.personId, split.materialId, { body: next, snippet: next.slice(0, 60), happenedOn: editDate });
      try {
        const updated = await api.patchMaterial(split.materialId, { body: next, snippet: next.slice(0, 60), happenedOn: editDate }, split.version);
        if (updated?.version != null) split.version = updated.version;
        haptic('memorySeal', { tier });
        renderRead();
        whisper('已同步更新手帐', 2000);
      } catch (err) {
        split.body = prevBody;
        split.happenedOn = prevDate;
        patchMaterial(split.personId, split.materialId, { body: prevBody, happenedOn: prevDate });
        saveBtn.disabled = false;
        haptic('warning');
        whisper(err?.message || '保存失败，已还原', 2600);
      }
    };
    saveBtn.addEventListener('click', doSave);
    imeGuard = createImeGuard?.(ta, { onSubmit: doSave, isTouchDevice: true }) || null;
    clear(bodyHost);
    bodyHost.append(ta, h('div', { class: 'sheet-actions', style: { 'margin-top': '8px' } }, dateBtn, h('button', { type: 'button', class: 'sheet-btn quiet', text: '取消', onClick: renderRead }), saveBtn));
    requestAnimationFrame(() => ta.focus({ preventScroll: true }));
  };

  const doReassign = () => {
    actions.openPeopleSheet?.({
      title: '换给哪一位人物？',
      onSelect: async (target) => {
        if (!target || target.id === split.personId) return;
        const oldPid = split.personId;
        const oldName = personChip.textContent;
        removeMaterial(oldPid, split.materialId);
        personChip.textContent = target.nameCn;
        try {
          const updated = await api.patchMaterial(split.materialId, { personId: target.id }, split.version);
          split.personId = target.id;
          split.personName = target.nameCn;
          split.tier = target.tier;
          if (updated?.version != null) split.version = updated.version;
          actions.reloadMaterials?.(oldPid);
          actions.reloadMaterials?.(target.id);
          haptic('memorySeal', { tier: target.tier });
          whisper(`已换至「${target.nameCn}」的手帐`, 2200);
        } catch (err) {
          personChip.textContent = oldName;
          actions.reloadMaterials?.(oldPid);
          haptic('warning');
          whisper(err?.message || '换人失败，已回滚', 2600);
        }
      },
    });
  };

  const doDelete = async () => {
    const res = await mwNativeUI?.showSheet?.({
      style: 'actionSheet',
      title: '删除这条归档记录？',
      items: [
        { id: 'delete', label: '删除手帐', title: '删除手帐', role: 'destructive', destructive: true },
        { id: 'cancel', label: '取消', title: '取消', role: 'cancel', cancel: true },
      ],
    });
    if (res && res.selectedId !== 'delete') return;
    const backup = store.get().materials[split.personId]?.items?.find((m) => m.id === split.materialId) || null;
    removeMaterial(split.personId, split.materialId);
    row.style.display = 'none';
    haptic('warning');
    let undone = false;
    showUndoToast('已删除这条记录', () => {
      undone = true;
      row.style.display = '';
      if (backup) upsertMaterial(split.personId, backup);
    }, {
      onExpire: async () => {
        if (undone) return;
        try { await api.deleteMaterial(split.materialId); row.remove(); onRefresh(); } catch { row.style.display = ''; }
      },
    });
  };

  renderRead();
  row.append(
    h('div', { class: 'archive-split-head', style: { display: 'flex', 'align-items': 'center', 'justify-content': 'space-between', gap: '8px' } }, personChip, dateChip),
    bodyHost,
    h(
      'div',
      { class: 'archive-crud-row' },
      h('button', { type: 'button', class: 'archive-mini-btn', text: '改字', onClick: enterEdit }),
      h('button', { type: 'button', class: 'archive-mini-btn', text: '换人', onClick: doReassign }),
      h('button', { type: 'button', class: 'archive-mini-btn is-danger', text: '删除', onClick: doDelete }),
    ),
  );
  return row;
}

function renderAddSplitInline(run, rawDetailsEl, host, { onAdded }) {
  const captureId = run.captureId || run.id;
  if (rawDetailsEl) rawDetailsEl.open = true; // Auto-expand 原话 above form so user can read quote!
  const s = store.get();
  let targetPerson = s.people[run.splits?.[0]?.personId] || s.people[s.activePersonId] || Object.values(s.people)[0] || null;
  let happenedOn = todayIso();

  const personBtn = h('button', {
    type: 'button',
    class: 'archive-mini-btn',
    text: targetPerson ? `记给：${targetPerson.nameCn} ▾` : '选择人物 ▾',
    onClick: () => actions.openPeopleSheet?.({
      title: '补拆的这条记给谁？',
      onSelect: (p) => { targetPerson = p; personBtn.textContent = `记给：${p.nameCn} ▾`; },
    }),
  });
  const dateBtn = h('button', {
    type: 'button',
    class: 'archive-mini-btn',
    text: `${formatDateFullCn(happenedOn)} ▾`,
    onClick: () => openChineseDatePicker({
      initialIso: happenedOn,
      mode: 'past',
      title: '选择发生日期',
      onConfirm: (iso) => { happenedOn = iso; dateBtn.textContent = `${formatDateFullCn(iso)} ▾`; },
    }),
  });
  const ta = h('textarea', { class: 'mw-field-textarea archive-edit-input', rows: '2', maxlength: '2000', placeholder: '从上面原话里补记一条遗漏的细节…', 'aria-label': '补拆内容' });
  const saveBtn = h('button', { type: 'button', class: 'sheet-btn archive-save-btn', text: '收进手帐' });
  let imeGuard = null;

  const closeForm = () => { imeGuard?.destroy?.(); form.remove(); };
  const doCreate = async () => {
    const body = ta.value.trim();
    if (!body || !targetPerson) return whisper(targetPerson ? '写一句内容再保存' : '先选一位人物', 2000);
    saveBtn.disabled = true;
    try {
      await createCaptureSlipRequest(captureId, {
        personId: targetPerson.id,
        body,
        happenedOn,
        inheritCaptureAudio: Boolean(run.hasAudio || run.audioUrl),
      });
      haptic('memorySeal', { tier: targetPerson.tier });
      actions.reloadMaterials?.(targetPerson.id);
      closeForm();
      whisper(`已补拆并收进「${targetPerson.nameCn}」`, 2200);
      onAdded();
    } catch (err) {
      saveBtn.disabled = false;
      haptic('warning');
      whisper(err?.message || '补拆失败', 2600);
    }
  };
  saveBtn.addEventListener('click', doCreate);
  imeGuard = createImeGuard?.(ta, { onSubmit: doCreate, isTouchDevice: true }) || null;

  const form = h('div', { class: 'archive-split-item archive-add-form' }, h('div', { class: 'archive-crud-row' }, personBtn, dateBtn), ta, h('div', { class: 'sheet-actions' }, h('button', { type: 'button', class: 'sheet-btn quiet', text: '取消', onClick: closeForm }), saveBtn));
  host.appendChild(form);
  requestAnimationFrame(() => ta.focus({ preventScroll: true }));
}

function renderRunCard(run, { onRefresh }) {
  const captureId = run.captureId || run.id;
  const transcript = run.transcript || run.rawText || '';
  const durMs = run.durationMs || run.audioDurationMs || 0;
  const canUndo = Boolean(run.canQuickUndo || (run.undoDeadline && new Date(run.undoDeadline).getTime() > Date.now() + 1000));
  const rawDetails = transcript
    ? h('details', { class: 'peek-verbatim-box archive-raw-details' }, h('summary', { text: '原话' }), h('p', { class: 'archive-raw-text', text: transcript }))
    : null;
  const addHost = h('div', { class: 'archive-add-host' });

  const splits = run.splits || [];
  const card = h(
    'article',
    { class: 'archive-run-card', dataset: { captureId } },
    h(
      'div',
      { class: 'archive-run-head' },
      h('span', { class: 'archive-source-tag', text: run.source === 'voice' ? '语音归档' : run.source === 'image' ? '图片归档' : '文字归档' }),
      h('span', { class: 'archive-time', text: formatDateCn(run.createdAt) }),
      canUndo
        ? h('button', {
            type: 'button',
            class: 'archive-mini-btn is-danger',
            text: '整条撤回',
            onClick: async () => {
              const touched = splits.map((s) => s.personId).filter(Boolean);
              await undoCapture(captureId, touched);
              onRefresh();
            },
          })
        : null,
    ),
    run.audioUrl ? h('div', { class: 'archive-audio-row' }, renderAudioPill({ id: captureId, audioUrl: run.audioUrl, audioDurationMs: durMs })) : null,
    rawDetails,
    h('div', { class: 'archive-splits-list', style: { display: 'flex', 'flex-direction': 'column', gap: '8px' } }, ...splits.map((sp) => renderSplitItem(sp, run, { onRefresh }))),
    addHost,
    h(
      'div',
      { class: 'archive-run-foot' },
      h('button', {
        type: 'button',
        class: 'sheet-btn quiet mw-btn-full archive-add-btn',
        text: '+ 从这段话补拆一条',
        onClick: () => {
          if (addHost.children.length) return;
          renderAddSplitInline(run, rawDetails, addHost, { onAdded: onRefresh });
        },
      }),
    ),
  );
  return card;
}

export function openArchiveHistory({ filter = 'all' } = {}) {
  let activeFilter = filter;
  let items = [];
  let nextCursor = null;
  let loading = false;
  const ac = new AbortController();

  const root = h('div', { class: 'archive-sheet-root', style: { display: 'flex', 'flex-direction': 'column', gap: '12px' } });
  const filterChips = [];
  const filterBar = h(
    'div',
    { class: 'archive-filter-bar', role: 'tablist', 'aria-label': '筛选最近归档' },
    ...FILTERS.map((f) => {
      const btn = h('button', {
        type: 'button',
        role: 'tab',
        class: ['archive-filter-chip', activeFilter === f.id && 'is-selected'],
        'aria-selected': String(activeFilter === f.id),
        text: f.label,
        onClick: () => {
          haptic('selection');
          activeFilter = f.id;
          filterChips.forEach((c) => {
            const on = c.dataset.filter === activeFilter;
            c.classList.toggle('is-selected', on);
            c.setAttribute('aria-selected', String(on));
          });
          load(false);
        },
      });
      btn.dataset.filter = f.id;
      filterChips.push(btn);
      return btn;
    }),
  );
  const listHost = h('div', { class: 'archive-list-host', style: { display: 'flex', 'flex-direction': 'column', gap: '12px' } });
  root.append(filterBar, listHost);

  async function renderList() {
    if (ac.signal.aborted) return;
    const localDrafts = await (draftQueue.listAll ? draftQueue.listAll() : draftQueue.list()).catch(() => []);
    clear(listHost);
    const outboxEl = renderLocalOutboxSection(localDrafts, () => load(false));
    if (outboxEl) listHost.appendChild(outboxEl);

    const visible = activeFilter === 'pending'
      ? items.filter((it) => (it.pendingCount || 0) > 0 || (it.splits || []).some((sp) => sp.presentation === 'pencil'))
      : items;

    if (!visible.length && !localDrafts.length) {
      listHost.appendChild(h('p', { class: 'archive-empty', text: loading ? '正在读取归档记录…' : '这里还没有归档记录' }));
      return;
    }
    for (const run of visible) listHost.appendChild(renderRunCard(run, { onRefresh: () => load(false) }));
    if (nextCursor) {
      listHost.appendChild(h('button', { type: 'button', class: 'load-more-btn', text: '载入更早的归档', onClick: () => load(true) }));
    }
  }

  async function load(append) {
    if (loading) return;
    loading = true;
    if (!append) { items = []; nextCursor = null; renderList(); }
    try {
      const res = await listCapturesRequest({ cursor: append ? nextCursor : undefined, limit: 20, filter: activeFilter });
      const fresh = res?.items || [];
      items = append ? [...items, ...fresh] : fresh;
      nextCursor = res?.nextCursor || null;
    } catch (err) {
      whisper(err?.message || '未能载入归档记录', 2600);
    } finally {
      loading = false;
      renderList();
    }
  }

  load(false);
  return openSheet({
    title: '最近归档记录',
    sub: '随时回听原话、改字、换人或补拆遗漏条目',
    body: root,
    className: 'is-archive-history-sheet',
    onClose: () => { ac.abort(); stopActiveAudio(); },
  });
}
