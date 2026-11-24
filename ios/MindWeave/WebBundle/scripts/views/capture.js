// capture.js — Write-Ahead Outbox (draftQueue.add() BEFORE fetch), 5-signal wake (network.ready),
// PHPicker zero-OOM attachment (mwNativeUI.pickPhoto, disabled on Silver), activeObjectUrls LK-6
// backstop, neutral subway top pill on Silver global mode, & 0-3s Expandable Routing Receipt.
import { h, clear, svg, $ } from '../dom.js';
import { api, uuid } from '../api/endpoints.js';
import { isRetryableError } from '../api/client.js';
import { store, addPending, removePending, upsertMaterial } from '../store.js';
import { createDraftQueue, idbStorage, memoryStorage } from '../offline-queue.js';
import { extensionFor } from '../voice-logic.js';
import { TIER_LABEL, clip } from '../format.js';
import { monogramOf, sealStyle } from '../decor.js';
import { CAMERA_SVG } from '../icons.js';
import { whisper } from './toast.js';
import { openSheet, closeSheet } from './sheet.js';
import { actions } from '../actions.js';
import { haptic, onNativeEvent, mwSpeech, mwNativeUI } from '../native-bridge.js';
import { createImeGuard } from '../lib/ime-guard.js';
import { openCaptureReceipt } from './capture-receipt.js';

const OFFLINE_COPY = '已安全暂存本机，联网后自动整理入册';
const activeObjectUrls = new Set();
let wakeAc = null;
let flushTimer = 0;
let subwayPillEl = null;

function trackObjectUrl(url) {
  if (url && String(url).startsWith('blob:')) activeObjectUrls.add(url);
  return url;
}

function revokeObjectUrl(url) {
  if (!url) return;
  activeObjectUrls.delete(url);
  if (String(url).startsWith('blob:')) {
    try { URL.revokeObjectURL(url); } catch { /* ignore */ }
  }
}

if (typeof window !== 'undefined') {
  window.addEventListener('pagehide', () => {
    for (const u of activeObjectUrls) {
      try { URL.revokeObjectURL(u); } catch { /* ignore */ }
    }
    activeObjectUrls.clear();
  });
}

function sendDraft(d) {
  if (d.kind === 'voice') {
    return api.ingestVoice(
      {
        id: d.id,
        blob: d.blob,
        filename: d.filename || `voice.${extensionFor(d.blob?.type || d.mime)}`,
        routeMode: d.routeMode,
        activePersonId: d.activePersonId,
        clientTranscript: d.clientTranscript,
        speechEngine: d.speechEngine,
        audioDurationMs: d.audioDurationMs,
      },
      d.idemKey,
    );
  }
  return api.captureText(
    { id: d.id, text: d.text, routeMode: d.routeMode, activePersonId: d.activePersonId },
    d.idemKey,
  );
}

let storage;
try {
  storage = globalThis.indexedDB ? idbStorage() : memoryStorage();
} catch {
  storage = memoryStorage();
}
export const draftQueue = createDraftQueue(storage, { send: sendDraft });

function setPendingState(id, patch) {
  store.set((s) => ({ pending: s.pending.map((p) => (p.id === id ? { ...p, ...patch } : p)) }));
}

function renderSubwayPill(count) {
  const host = $('#journalCanvas') || $('#app');
  if (!host) return;
  if (!count) {
    if (subwayPillEl) { subwayPillEl.style.display = 'none'; subwayPillEl.remove(); subwayPillEl = null; }
    return;
  }
  if (!subwayPillEl || !subwayPillEl.isConnected) {
    subwayPillEl = h('button', {
      type: 'button',
      class: 'offline-subway-pill',
      onClick: () => { haptic('light'); actions.openArchiveHistory?.({ filter: 'pending' }); },
    });
    host.appendChild(subwayPillEl);
  }
  subwayPillEl.textContent = `${count} 条离线语音已安全暂存，联网后自动分记`;
  subwayPillEl.style.display = 'inline-flex';
}

export async function refreshDraftCount() {
  try {
    const items = await draftQueue.list();
    const count = items.length;
    store.set({ draftCount: count });
    const hasGlobal = items.some((d) => d.routeMode === 'global' || !d.activePersonId);
    renderSubwayPill(hasGlobal ? count : 0);
  } catch { /* ignore */ }
}

/** Write-Ahead Outbox: persist to IDB (< 3ms) BEFORE network fetch (BC2 BUG-SQ-4). */
async function submit(draft, { optimisticText }) {
  const s = store.get();
  const activeP = s.activePersonId ? s.people[s.activePersonId] : null;
  // J2-S3 Silver Isolation: NEVER inject unrouted global-mode draft into Silver timeline!
  const hideFromSilver = Boolean(activeP?.tier === 'silver' && draft.routeMode === 'global');
  const pageId = hideFromSilver ? null : (s.view === 'journal' ? s.activePersonId : null);

  try {
    await draftQueue.add(draft);
    refreshDraftCount();
  } catch { /* fallback to network attempt */ }

  if (!hideFromSilver) {
    addPending({ id: draft.id, pageId, text: optimisticText, state: 'drying', kind: draft.kind });
  }

  try {
    const res = await sendDraft(draft);
    await Promise.resolve((draftQueue.discardDraft || draftQueue.remove)?.(draft.id)).catch(() => {});
    try { await storage.delete(draft.id); } catch { /* ignore */ }
    if (draft.nativeDraftId) mwSpeech?.acknowledgeDraftUploaded?.(draft.nativeDraftId);
    removePending(draft.id);
    refreshDraftCount();
    handleCaptureResult(res, { captureId: res?.captureId || res?.jobId });
    return res;
  } catch (err) {
    if (err?.status === 401) {
      // Never wipe IDB drafts on involuntary 401!
      if (!hideFromSilver) setPendingState(draft.id, { state: 'queued', text: draft.clientTranscript || draft.text || '一段语音' });
      whisper('登录已过期，本机草稿已保住，重新登录后自动补传', 3400);
      refreshDraftCount();
      return null;
    }
    if (isRetryableError(err)) {
      if (!hideFromSilver) setPendingState(draft.id, { state: 'queued', text: draft.clientTranscript || draft.text || '一段语音' });
      whisper(OFFLINE_COPY, 3200);
      refreshDraftCount();
      return null;
    }
    // Terminal 4xx -> quarantine instead of silent delete (Invariant I2)
    try {
      await storage.put({ ...draft, status: 'quarantined', lastErrorCode: err?.code || String(err?.status || 'error') });
    } catch { /* ignore */ }
    setPendingState(draft.id, { state: 'failed', text: err?.message || '未能入册' });
    setTimeout(() => removePending(draft.id), 4200);
    whisper(err?.message || '这条记录未能入册，可在最近归档里重试', 3600);
    refreshDraftCount();
    return null;
  }
}

export function submitVoice({ blob, audioUrl = null, draftId = null, clientTranscript = '', speechEngine = '', audioDurationMs = 0, route }) {
  const draft = {
    id: uuid(),
    idemKey: uuid(),
    kind: 'voice',
    blob,
    audioUrl,
    nativeDraftId: draftId,
    clientTranscript,
    speechEngine,
    audioDurationMs,
    recordedAt: new Date().toISOString(),
    routeMode: route?.routeMode || 'global',
    activePersonId: route?.activePersonId || null,
    status: 'pending',
  };
  return submit(draft, { optimisticText: clientTranscript || '正在把这段话整理入册…' });
}

export function submitText({ text, route }) {
  haptic('light');
  const draft = {
    id: uuid(),
    idemKey: uuid(),
    kind: 'text',
    text,
    recordedAt: new Date().toISOString(),
    routeMode: route?.routeMode || 'global',
    activePersonId: route?.activePersonId || null,
    status: 'pending',
  };
  return submit(draft, { optimisticText: text });
}

export async function submitImage({ file, text = '', route }) {
  haptic('light');
  const id = uuid();
  const pageId = store.get().view === 'journal' ? store.get().activePersonId : null;
  addPending({ id, pageId, text: text || '正在整理这张图片…', state: 'drying', kind: 'image' });
  try {
    const res = await api.captureImage(
      { id, file, text: text || undefined, routeMode: route.routeMode, activePersonId: route.activePersonId || null },
      uuid(),
    );
    removePending(id);
    handleCaptureResult(res, { captureId: res?.captureId || res?.jobId });
    return res;
  } catch (err) {
    setPendingState(id, { state: 'failed', text: err?.message || '上传失败' });
    setTimeout(() => removePending(id), 4200);
    whisper(err?.message || '图片未能上传', 3400);
    return null;
  }
}

export async function submitPhotoMaterial({ personId, file, caption = '' }) {
  const person = store.get().people[personId];
  if (person?.tier === 'silver') {
    whisper('银盾边界页默认不存放照片', 2400);
    return null;
  }
  haptic('light');
  const id = uuid();
  addPending({ id, pageId: personId, text: caption || '正在收好这张照片…', state: 'drying', kind: 'photo' });
  try {
    const created = await api.uploadPhoto(personId, { file, caption, pinned: false });
    removePending(id);
    if (created?.id) upsertMaterial(personId, created);
    else actions.reloadMaterials?.(personId);
    haptic('memorySeal', { tier: person?.tier || 'gold' });
    whisper('照片已收进手帐', 2400);
    return created;
  } catch (err) {
    setPendingState(id, { state: 'failed', text: err?.message || '照片上传失败' });
    setTimeout(() => removePending(id), 4200);
    whisper(err?.message || '照片未能保存', 3400);
    return null;
  }
}

function uniqueSplits(splits) {
  const seen = new Map();
  for (const s of splits || []) if (s.personId && !seen.has(s.personId)) seen.set(s.personId, s);
  return [...seen.values()];
}

export function handleCaptureResult(res, { captureId, quiet = false } = {}) {
  if (!res) return;
  if (res.safetyCategory === 'crisis' || res.crisisCard) {
    (actions.openCrisisCard || actions.showCrisis)?.(res.crisisCard || { category: 'crisis' });
    return;
  }
  const splits = res.splits || [];
  const people = uniqueSplits(splits);
  const touched = people.map((s) => s.personId);
  const anyNew = splits.some((s) => s.isNewPerson) || touched.some((id) => !store.get().people[id]);
  if (anyNew) actions.refreshBootstrap?.();
  for (const id of touched) actions.reloadMaterials?.(id);
  if (quiet) return;

  const cid = captureId || res.captureId || res.jobId || null;
  const openReceipt = actions.openCaptureReceipt || openCaptureReceipt;
  openReceipt(res, { captureId: cid, onUndo: cid ? () => undoCapture(cid, touched) : null });
}

export async function undoCapture(captureId, touched = []) {
  haptic('warning');
  try {
    await api.undoCapture(captureId);
    whisper('已撤回，这段话没有留下', 2400);
  } catch (err) {
    whisper(err?.status === 409 ? '撤销窗口已过，可在最近归档里修改' : (err?.message || '撤回失败'), 2800);
  }
  for (const id of touched) actions.reloadMaterials?.(id);
  actions.refreshBootstrap?.();
}

export async function flushDrafts() {
  const result = await draftQueue.flush().catch(() => null);
  if (!result) return;
  for (const { draft, result: res } of result.sent || []) {
    if (draft.nativeDraftId) mwSpeech?.acknowledgeDraftUploaded?.(draft.nativeDraftId);
    removePending(draft.id);
    handleCaptureResult(res, { quiet: true });
  }
  if (result.sent?.length) whisper(`本机离线草稿已自动补传 ${result.sent.length} 条`, 2800);
  refreshDraftCount();
}

/** 5-signal wake (online, visibilitychange, pageshow, focus, Swift network.ready) with single AbortController. */
export function initCapture() {
  wakeAc?.abort();
  wakeAc = new AbortController();
  const sig = wakeAc.signal;
  const wake = () => flushDrafts();
  globalThis.addEventListener?.('online', wake, { signal: sig });
  globalThis.addEventListener?.('pageshow', wake, { signal: sig });
  globalThis.addEventListener?.('focus', wake, { signal: sig });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') wake();
  }, { signal: sig });
  onNativeEvent?.('network.ready', wake, { signal: sig });

  refreshDraftCount();
  clearTimeout(flushTimer);
  flushTimer = setTimeout(wake, 1200);
}

/* ---------------- Text + PHPicker Zero-OOM Attachment Sheet ---------------- */
export function openTextCapture({ personId = null, prompt = '', directory = false } = {}) {
  const person = personId ? store.get().people[personId] : null;
  const tier = person?.tier || 'crystal';
  const allowPhoto = Boolean(person && tier !== 'silver' && !directory);
  let mode = person && !directory ? 'current' : 'global';
  let selectedFiles = [];
  let previewUrls = [];
  let imageKind = 'capture';
  let imeGuard = null;

  const textarea = h('textarea', {
    class: 'capture-textarea',
    id: 'captureText',
    rows: '3',
    maxlength: '2000',
    'aria-label': '想记下的一句话',
    placeholder: prompt ? `${prompt} 写一句就好` : person ? `写一句关于「${person.nameCn}」的事…` : '写一句发生的事，丘比特会帮你整理入册…',
  });
  const previewHost = h('div', { class: 'capture-image-preview', style: { display: 'none' } });
  const sendBtn = h('button', { type: 'button', class: 'sheet-btn', id: 'captureSend', text: '收进手帐', disabled: true });

  const syncSendEnabled = () => {
    sendBtn.disabled = !textarea.value.trim() && !selectedFiles.length;
  };
  const revokeAll = () => {
    for (const u of previewUrls) revokeObjectUrl(u);
    previewUrls = [];
  };
  const clearImages = () => {
    selectedFiles = [];
    revokeAll();
    clear(previewHost);
    previewHost.style.display = 'none';
    syncSendEnabled();
  };

  const renderPreview = () => {
    clear(previewHost);
    if (!selectedFiles.length) { previewHost.style.display = 'none'; return; }
    previewHost.style.display = 'flex';
    const first = selectedFiles[0];
    const modeRow = allowPhoto
      ? h(
          'div',
          { class: 'capture-img-modes', role: 'radiogroup', 'aria-label': '图片用途' },
          ...[['capture', '识别文字'], ['photo', '收作照片']].map(([k, label]) =>
            h('button', {
              type: 'button',
              class: ['img-mode-chip', imageKind === k && 'is-selected'],
              role: 'radio',
              'aria-checked': String(imageKind === k),
              text: label,
              onClick: () => { imageKind = k; renderPreview(); },
            }),
          ),
        )
      : null;
    previewHost.append(
      h('img', { class: 'capture-preview-thumb', src: previewUrls[0], alt: '已选图片预览' }),
      h('div', { class: 'capture-preview-meta' }, h('span', { class: 'capture-preview-name', text: selectedFiles.length > 1 ? `已选 ${selectedFiles.length} 张图` : clip(first.name || '已选一张图', 24) }), modeRow),
      h('button', { type: 'button', class: 'capture-preview-clear', 'aria-label': '移除已选图片', text: '×', onClick: clearImages }),
    );
  };

  const pickImages = async () => {
    haptic('light');
    try {
      const picked = await mwNativeUI.pickPhoto({ maxCount: 4, maxPixelSize: 1600, tier });
      if (!Array.isArray(picked) || !picked.length) return;
      revokeAll();
      selectedFiles = picked.map((p) => (p instanceof Blob ? p : p?.file)).filter(Boolean);
      previewUrls = picked
        .map((p) => (p instanceof Blob ? trackObjectUrl(URL.createObjectURL(p)) : p?.url || (p?.file ? trackObjectUrl(URL.createObjectURL(p.file)) : '')))
        .filter(Boolean);
      imageKind = allowPhoto ? 'photo' : 'capture';
      renderPreview();
      syncSendEnabled();
    } catch {
      whisper('这次没能选到图片', 2200);
    }
  };

  const imgBtn = tier !== 'silver'
    ? h('button', { type: 'button', class: 'capture-image-btn', id: 'captureImageBtn', onClick: pickImages }, svg(CAMERA_SVG, 'capture-image-icon'), h('span', { text: '选照片 / 截图' }))
    : null;

  const chips = [];
  const routeRow = person
    ? h(
        'div',
        { class: 'capture-route-toggle', role: 'radiogroup', 'aria-label': '归档去向' },
        ...[['current', `收进「${person.nameCn}」`], ['global', '自动分记']].map(([value, label]) => {
          const chip = h('button', {
            type: 'button',
            class: ['route-chip', mode === value && 'is-selected'],
            role: 'radio',
            'aria-checked': String(mode === value),
            text: label,
            onClick: () => {
              mode = value;
              chips.forEach((c) => {
                const on = c.dataset.value === value;
                c.classList.toggle('is-selected', on);
                c.setAttribute('aria-checked', String(on));
              });
            },
          });
          chip.dataset.value = value;
          chips.push(chip);
          return chip;
        }),
      )
    : h('p', { class: 'sheet-sub', text: '丘比特会辨认提及的人物，自动整理进对应手帐。' });

  const doSend = () => {
    const text = textarea.value.trim();
    if (!text && !selectedFiles.length) return;
    const files = [...selectedFiles];
    const kind = imageKind;
    const route = mode === 'current' && person ? { routeMode: 'current', activePersonId: person.id } : { routeMode: 'global', activePersonId: null };
    closeSheet();
    if (files.length) {
      for (const file of files) {
        if (kind === 'photo' && allowPhoto && person) submitPhotoMaterial({ personId: person.id, file, caption: text });
        else submitImage({ file, text, route });
      }
      return;
    }
    submitText({ text, route });
  };

  textarea.addEventListener('input', syncSendEnabled);
  imeGuard = createImeGuard?.(textarea, { onSubmit: doSend, isTouchDevice: true }) || null;
  sendBtn.addEventListener('click', doSend);

  openSheet({
    title: prompt || '记一句',
    body: [
      person
        ? h('span', { class: 'capture-scope' }, h('span', { class: ['mono-seal', tier === 'silver' && 'is-cloaked'], style: tier === 'silver' ? {} : sealStyle(person), text: monogramOf(person) }), h('span', { text: `${person.nameCn} · ${TIER_LABEL?.[tier]?.short || ''}` }))
        : null,
      textarea,
      previewHost,
      routeRow,
      h('div', { class: 'sheet-actions is-split' }, imgBtn, h('div', { class: 'sheet-actions-right' }, h('button', { type: 'button', class: 'sheet-btn quiet', text: '算了', onClick: () => closeSheet() }), sendBtn)),
    ],
    initialFocus: textarea,
    onClose: () => { imeGuard?.destroy?.(); revokeAll(); },
  });
}

/** Kept as lightweight no-op/compat export for main.js action bindings (replaced by Expandable Receipt). */
export function flyAvatars() {}
