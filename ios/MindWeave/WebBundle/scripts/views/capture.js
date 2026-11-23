// capture.js — everything that turns a sentence (voice or text) into journal ink:
// optimistic "drying ink" slip → API → real splits → fly avatars + undo toast.
// Offline / flaky network → IndexedDB draft queue, flushed on `online` with the same Idempotency-Key.
import { h, clear, svg, $ } from '../dom.js';
import { api, uuid } from '../api/endpoints.js';
import { isRetryableError } from '../api/client.js';
import { store, addPending, removePending, upsertMaterial } from '../store.js';
import { createDraftQueue, idbStorage, memoryStorage } from '../offline-queue.js';
import { extensionFor } from '../voice-logic.js';
import { namesList, TIER_LABEL, shouldSendOnEnter, clip } from '../format.js';
import { monogramOf, sealStyle } from '../decor.js';
import { CAMERA_SVG } from '../icons.js';
import { showToast, whisper } from './toast.js';
import { openSheet, closeSheet } from './sheet.js';
import { actions } from '../actions.js';
import { haptic } from '../native-bridge.js';

const OFFLINE_COPY = '已存入本地草稿，联网后自动补传';

function sendDraft(d) {
  if (d.kind === 'voice') {
    return api.ingestVoice(
      { id: d.id, blob: d.blob, filename: `voice.${extensionFor(d.blob?.type)}`, routeMode: d.routeMode, activePersonId: d.activePersonId },
      d.idemKey,
    );
  }
  return api.captureText({ id: d.id, text: d.text, routeMode: d.routeMode, activePersonId: d.activePersonId }, d.idemKey);
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

async function refreshDraftCount() {
  try {
    store.set({ draftCount: await draftQueue.count() });
  } catch {
    /* ignore */
  }
}

async function queueDraft(draft, pendingId) {
  try {
    await draftQueue.add(draft);
    setPendingState(pendingId, { state: 'queued', text: draft.text || '一段语音' });
    whisper(OFFLINE_COPY, 3600);
  } catch {
    setPendingState(pendingId, { state: 'failed' });
    whisper('网络有点慢，这句话没能存下，稍后再说一次吧', 3600);
  }
  refreshDraftCount();
}

/** Common pipeline for voice + text. */
async function submit(draft, { optimisticText }) {
  const pageId = store.get().view === 'journal' ? store.get().activePersonId : null;
  addPending({ id: draft.id, pageId, text: optimisticText, state: 'drying', kind: draft.kind });
  // Always try the network first: navigator.onLine is unreliable (false negatives behind VPNs,
  // sandboxes, some Android WebViews). A real network failure still lands in the draft queue.
  try {
    const res = await sendDraft(draft);
    removePending(draft.id);
    handleCaptureResult(res, { captureId: res.captureId || res.jobId });
    return res;
  } catch (err) {
    if (isRetryableError(err)) {
      await queueDraft(draft, draft.id);
      return null;
    }
    setPendingState(draft.id, { state: 'failed', text: err.message });
    setTimeout(() => removePending(draft.id), 4200);
    whisper(err.message, 3600);
    return null;
  }
}

export function submitVoice({ blob, route }) {
  haptic('light');
  const draft = { id: uuid(), idemKey: uuid(), kind: 'voice', blob, routeMode: route.routeMode, activePersonId: route.activePersonId || null };
  return submit(draft, { optimisticText: '正在把这段话誊进手帐…' });
}

export function submitText({ text, route }) {
  haptic('light');
  const draft = { id: uuid(), idemKey: uuid(), kind: 'text', text, routeMode: route.routeMode, activePersonId: route.activePersonId || null };
  return submit(draft, { optimisticText: text });
}

export async function submitImage({ file, text = '', route }) {
  haptic('light');
  const id = uuid();
  const pageId = store.get().view === 'journal' ? store.get().activePersonId : null;
  addPending({ id, pageId, text: text || '正在细看这张截图…', state: 'drying', kind: 'image' });
  try {
    const res = await api.captureImage(
      { id, file, text: text || undefined, routeMode: route.routeMode, activePersonId: route.activePersonId || null },
      uuid(),
    );
    removePending(id);
    handleCaptureResult(res, { captureId: res.captureId || res.jobId });
    return res;
  } catch (err) {
    setPendingState(id, { state: 'failed', text: err.message });
    setTimeout(() => removePending(id), 4200);
    whisper(err.message, 3400);
    return null;
  }
}

export async function submitPhotoMaterial({ personId, file, caption = '' }) {
  haptic('light');
  const id = uuid();
  addPending({ id, pageId: personId, text: caption || '正在把照片贴进手帐…', state: 'drying', kind: 'photo' });
  try {
    const created = await api.uploadPhoto(personId, { file, caption, pinned: false });
    removePending(id);
    if (created?.id) upsertMaterial(personId, created);
    else actions.reloadMaterials(personId);
    whisper('照片贴进手帐啦', 2600);
    return created;
  } catch (err) {
    setPendingState(id, { state: 'failed', text: err.message });
    setTimeout(() => removePending(id), 4200);
    whisper(err.message, 3400);
    return null;
  }
}

function uniqueSplits(splits) {
  const seen = new Map();
  for (const s of splits || []) if (s.personId && !seen.has(s.personId)) seen.set(s.personId, s);
  return [...seen.values()];
}

/** Apply a capture/ingest response to the UI. Quiet mode skips the undo toast (draft flush). */
export function handleCaptureResult(res, { captureId, quiet = false } = {}) {
  if (!res) return;
  if (res.crisisCard) {
    actions.showCrisis(res.crisisCard);
    return;
  }
  const splits = res.splits || [];
  const people = uniqueSplits(splits);
  const touched = people.map((s) => s.personId);
  const anyNew = splits.some((s) => s.isNewPerson) || touched.some((id) => !store.get().people[id]);
  if (anyNew) actions.refreshBootstrap();
  for (const id of touched) actions.reloadMaterials(id);

  const active = store.get().view === 'journal' ? store.get().activePersonId : null;
  if (people.length > 1 || (people.length === 1 && people[0].personId !== active)) actions.flyAvatars(people);
  if (quiet) return;

  const names = people.map((s) => s.personName || store.get().people[s.personId]?.nameCn).filter(Boolean);
  const allPencil = splits.length > 0 && splits.every((s) => s.presentation && s.presentation !== 'ink');
  if (res.undoDeadline && captureId) {
    const msLeft = Math.max(1500, Math.min(5000, new Date(res.undoDeadline).getTime() - Date.now()));
    showToast(names.length ? `已收进${namesList(names)}的手帐` : '已收进手帐', {
      actionLabel: '撤销',
      ms: Number.isFinite(msLeft) ? msLeft : 5000,
      onAction: () => undoCapture(captureId, touched),
    });
  } else if (allPencil) {
    whisper('先用铅笔记下了，等你看一眼', 3200);
  } else if (!splits.length) {
    whisper('这句话先收在待整理里了', 3200);
  } else {
    whisper(names.length ? `已收进${namesList(names)}的手帐` : '已收进手帐', 3000);
  }
}

export async function undoCapture(captureId, touched = []) {
  haptic('warning');
  try {
    await api.undoCapture(captureId);
    whisper('已撤回，这句话没有留下', 2600);
  } catch (err) {
    whisper(err.status === 409 ? '已经落墨了，撤销的时间过了' : err.message, 3000);
  }
  for (const id of touched) actions.reloadMaterials(id);
  actions.refreshBootstrap();
}

export async function flushDrafts() {
  const result = await draftQueue.flush().catch(() => null);
  if (!result) return;
  for (const { draft, result: res } of result.sent) {
    removePending(draft.id);
    handleCaptureResult(res, { quiet: true });
  }
  for (const { draft } of result.dropped) removePending(draft.id);
  if (result.sent.length) whisper(`本地草稿已补传 ${result.sent.length} 条`, 2800);
  refreshDraftCount();
}

export function initCapture() {
  globalThis.addEventListener?.('online', () => flushDrafts());
  refreshDraftCount();
  setTimeout(() => flushDrafts(), 1200);
}

/* ---------------- text + photo/screenshot capture sheet ---------------- */
export function openTextCapture({ personId = null, prompt = '', directory = false } = {}) {
  const person = personId ? store.get().people[personId] : null;
  const allowPhoto = Boolean(person && person.tier !== 'silver' && !directory);
  let mode = person && !directory ? 'current' : 'global';
  let selectedFile = null;
  let previewUrl = null;
  let imageKind = 'capture'; // capture (OCR/extract) | photo (Polaroid slip)

  const textarea = h('textarea', {
    class: 'capture-textarea',
    id: 'captureText',
    rows: '3',
    maxlength: '2000',
    'aria-label': '想记下的一句话',
    placeholder: prompt ? `${prompt} 说一句就好` : '比如：她不吃香菜，生日是 5 月 12 日',
  });
  const fileInput = h('input', {
    type: 'file',
    id: 'captureImageInput',
    accept: 'image/*',
    hidden: true,
    'aria-label': '选择照片或截图',
  });
  const previewHost = h('div', { class: 'capture-image-preview', hidden: true });
  const sendBtn = h('button', { type: 'button', class: 'sheet-btn', id: 'captureSend', text: '收进手帐', disabled: true });

  const syncSendEnabled = () => {
    sendBtn.disabled = !textarea.value.trim() && !selectedFile;
  };
  const revokePreview = () => {
    if (previewUrl) {
      URL.revokeObjectURL(previewUrl);
      previewUrl = null;
    }
  };
  const clearImage = () => {
    selectedFile = null;
    fileInput.value = '';
    revokePreview();
    clear(previewHost);
    previewHost.hidden = true;
    syncSendEnabled();
  };

  const renderPreview = () => {
    clear(previewHost);
    if (!selectedFile) {
      previewHost.hidden = true;
      return;
    }
    previewHost.hidden = false;
    const modeRow = allowPhoto
      ? h(
          'div',
          { class: 'capture-img-modes', role: 'radiogroup', 'aria-label': '图片用途' },
          ...[
            ['capture', '识别截图'],
            ['photo', '贴成照片'],
          ].map(([k, label]) =>
            h('button', {
              type: 'button',
              class: ['img-mode-chip', imageKind === k && 'is-selected'],
              role: 'radio',
              'aria-checked': String(imageKind === k),
              text: label,
              onClick: () => {
                imageKind = k;
                renderPreview();
              },
            }),
          ),
        )
      : null;
    previewHost.append(
      h('img', { class: 'capture-preview-thumb', src: previewUrl, alt: '已选图片预览' }),
      h(
        'div',
        { class: 'capture-preview-meta' },
        h('span', { class: 'capture-preview-name', text: clip(selectedFile.name || '已选一张图', 24) }),
        modeRow,
      ),
      h('button', { type: 'button', class: 'capture-preview-clear', 'aria-label': '去掉这张图', text: '×', onClick: clearImage }),
    );
  };

  fileInput.addEventListener('change', () => {
    const file = fileInput.files?.[0];
    if (!file) return;
    revokePreview();
    selectedFile = file;
    previewUrl = URL.createObjectURL(file);
    imageKind = 'capture';
    renderPreview();
    syncSendEnabled();
  });

  const imgBtn = h(
    'button',
    {
      type: 'button',
      class: 'capture-image-btn',
      id: 'captureImageBtn',
      onClick: () => fileInput.click(),
    },
    svg(CAMERA_SVG, 'capture-image-icon'),
    h('span', { text: '传照片 / 截图' }),
  );

  const chips = [];
  const routeRow = person
    ? h(
        'div',
        { class: 'capture-route-toggle', role: 'radiogroup', 'aria-label': '收到哪里' },
        ...[
          ['current', `收进「${person.nameCn}」`],
          ['global', '让丘比特自动分'],
        ].map(([value, label]) => {
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
    : h('p', { class: 'sheet-sub', text: '丘比特会听出是关于谁的，自动收进 TA 的手帐。' });

  const doSend = () => {
    const text = textarea.value.trim();
    if (!text && !selectedFile) return;
    const file = selectedFile;
    const kind = imageKind;
    const route = mode === 'current' && person ? { routeMode: 'current', activePersonId: person.id } : { routeMode: 'global', activePersonId: null };
    closeSheet();
    if (file) {
      if (kind === 'photo' && allowPhoto && person) submitPhotoMaterial({ personId: person.id, file, caption: text });
      else submitImage({ file, text, route });
      return;
    }
    submitText({ text, route });
  };
  textarea.addEventListener('input', syncSendEnabled);
  textarea.addEventListener('keydown', (e) => {
    if (shouldSendOnEnter(e)) {
      e.preventDefault();
      doSend();
    }
  });
  sendBtn.addEventListener('click', doSend);

  openSheet({
    title: prompt || '记一句',
    body: [
      person
        ? h(
            'span',
            { class: 'capture-scope' },
            h('span', { class: ['mono-seal', person.tier === 'silver' && 'is-cloaked'], style: person.tier === 'silver' ? {} : sealStyle(person), text: monogramOf(person) }),
            h('span', { text: `${person.nameCn} · ${TIER_LABEL[person.tier]?.short || ''}` }),
          )
        : null,
      textarea,
      fileInput,
      previewHost,
      routeRow,
      h(
        'div',
        { class: 'sheet-actions is-split' },
        imgBtn,
        h('div', { class: 'sheet-actions-right' }, h('button', { type: 'button', class: 'sheet-btn quiet', text: '算了', onClick: () => closeSheet() }), sendBtn),
      ),
    ],
    initialFocus: textarea,
    onClose: revokePreview,
  });
}

/** Fly small avatars from above the dock to each person (no modal). */
export function flyAvatars(splits) {
  const layer = $('#flyLayer');
  if (!layer) return;
  const capsule = $('#capsule');
  const layerRect = layer.getBoundingClientRect();
  const target = capsule ? capsule.getBoundingClientRect() : null;
  splits.slice(0, 5).forEach((s, i) => {
    const person = store.get().people[s.personId] || { nameCn: s.personName, tier: s.tier };
    const isSilver = person.tier === 'silver';
    const el = h(
      'div',
      {
        class: ['fly-avatar', isSilver && 'is-cloaked'],
        style: { ...(isSilver ? {} : sealStyle({ id: s.personId, ...person })), 'margin-left': `${(i - (splits.length - 1) / 2) * 50 - 20}px` },
      },
      monogramOf({ ...person, nameCn: person.nameCn || s.personName }),
      h('span', { class: 'fly-name', text: person.nameCn || s.personName || '' }),
    );
    layer.appendChild(el);
    setTimeout(() => {
      const r = el.getBoundingClientRect();
      const tx = target ? target.left + target.width / 2 - (r.left + r.width / 2) : 0;
      const ty = target ? target.top + target.height / 2 - (r.top + r.height / 2) : layerRect.height;
      el.classList.add('is-flying');
      el.style.transform = `translate(${tx}px, ${ty}px) scale(0.4)`;
    }, 700 + i * 120);
    setTimeout(() => el.remove(), 1600 + i * 120);
  });
}
