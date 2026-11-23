// journal-items.js — pure tactile scrapbook slips (big serif text + tiny date only),
// D1 pencil drafts (dashed graphite + dotted date), question sticky notes,
// optimistic drying-ink slips, and accessible grabber-drag iOS Peek Focus Overlay.
import { h, clear, svg, $ } from '../dom.js';
import { materialDecor, monogramOf, sealStyle } from '../decor.js';
import { formatDateCn, formatDateFullCn, todayIso, highlightParts } from '../format.js';
import { BRASS_CLIP_SVG } from '../icons.js';
import { api } from '../api/endpoints.js';
import { store, patchMaterial, removeMaterial } from '../store.js';
import { whisper } from './toast.js';
import { actions } from '../actions.js';
import { haptic, setAudioSession, prefersReducedMotion } from '../native-bridge.js';
import { attachSwipeDismiss } from '../gestures.js';

const PENCIL_COPY = {
  date_glance: '这个日期对吗？',
  low_confidence: '丘比特不太确定，看一眼？',
  speaker_unknown: '这是谁说的？看一眼',
  polarity_flip: '和之前记的不太一样，看一眼？',
  multi_person: '提到了好几个人，看一眼？',
  venting: '先放在冷静区',
  new_person: '这是新朋友吗？',
  ambiguous_person: '这是哪一位？',
};
const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]):not([type="hidden"]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

let playing = null;
let activePeek = null;

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

function voiceCharm(m) {
  const playable = Boolean(m.hasMedia && m.mediaUrl);
  const btn = h(
    'button',
    { type: 'button', class: 'voice-charm', disabled: !playable, 'aria-label': playable ? `播放语音 ${m.dur || ''}` : '语音' },
    h('span', { class: 'voice-charm-hole' }),
    h('span', { class: 'voice-charm-play', text: '▶' }),
    h('span', { class: 'voice-charm-bars' }, h('i'), h('i'), h('i'), h('i'), h('i'), h('i')),
    m.dur ? h('span', { text: m.dur }) : null,
  );
  if (playable) {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      haptic('light');
      if (playing && playing.btn === btn) return playing.audio.pause();
      if (playing) playing.audio.pause();
      const audio = new Audio(m.mediaUrl);
      playing = { audio, btn };
      btn.classList.add('is-playing');
      const done = () => {
        btn.classList.remove('is-playing');
        if (playing && playing.btn === btn) {
          playing = null;
          setAudioSession('deactivate');
        }
      };
      audio.addEventListener('pause', done);
      audio.addEventListener('ended', done);
      setAudioSession('playAndRecord');
      audio.play().catch(() => { done(); whisper('这段语音暂时放不出来', 2400); });
    });
  }
  return btn;
}

function renderHighlightedBody(body, query, className = 'mat-body') {
  const p = h('p', { class: className });
  for (const seg of highlightParts(body, query)) {
    p.appendChild(seg.hit ? h('mark', { class: 'spot-hit', text: seg.text }) : document.createTextNode(seg.text));
  }
  return p;
}

function inked(personId, m) {
  patchMaterial(personId, m.id, { presentation: 'ink', glanceHint: null, pencilReason: null, pencilFacts: [], inboxItemId: null });
}

function primaryPencilFact(m) {
  return (m.pencilFacts || []).find((f) => f.triggerDate) || (m.pencilFacts || [])[0] || null;
}

async function confirmPencilMaterial(personId, m, customDate) {
  const fact = primaryPencilFact(m);
  if (m.pencilReason === 'date_glance' && fact) {
    const changed = Boolean(customDate && customDate !== fact.triggerDate);
    await api.patchFact(fact.id, changed ? { triggerDate: customDate, status: 'confirmed' } : { status: 'confirmed' }, fact.version);
    inked(personId, m);
    haptic('success');
    return changed ? `改好了，${formatDateCn(customDate)}` : '好，已经落墨';
  }
  if (m.inboxItemId) {
    await api.resolveInbox(m.inboxItemId, customDate ? { action: 'confirm', triggerDate: customDate } : { action: 'confirm' });
    inked(personId, m);
    haptic('success');
    return '好，已经落墨';
  }
  for (const f of m.pencilFacts || []) {
    const body = customDate && f.id === fact?.id && customDate !== f.triggerDate
      ? { triggerDate: customDate, status: 'confirmed' }
      : { status: 'confirmed' };
    await api.patchFact(f.id, body, f.version);
  }
  inked(personId, m);
  haptic('success');
  return '好，已经落墨';
}

/** Localized Chinese serif date picker pill backed by native date input overlay. */
function chineseDatePill(initialIso, ariaLabel = '确认日期') {
  const val = initialIso || todayIso();
  const labelEl = h('span', { class: 'peek-date-label', text: formatDateFullCn(val) || formatDateCn(val) || val });
  const input = h('input', { type: 'date', class: 'peek-date-native', value: val, 'aria-label': ariaLabel });
  input.addEventListener('change', () => {
    const v = input.value || val;
    labelEl.textContent = formatDateFullCn(v) || formatDateCn(v) || v;
    haptic('selection');
  });
  const pill = h(
    'div',
    {
      class: 'peek-date-pill',
      onClick: () => {
        if (typeof input.showPicker === 'function') {
          try { input.showPicker(); } catch { /* native overlay handles click */ }
        }
      },
    },
    labelEl,
    h('span', { class: 'peek-date-caret', 'aria-hidden': 'true', text: '▾' }),
    input,
  );
  return { el: pill, input };
}

/** Understated dotted-underline date/glance button on pencil drafts (zero badge chrome). */
function pencilFooter(m, personId, date) {
  const wrap = h('div', { class: 'pencil-foot' });
  const reason = m.pencilReason || '';
  const fact = primaryPencilFact(m);
  const label = date || formatDateCn(fact?.triggerDate) || '待落墨';
  const chip = h('button', {
    type: 'button',
    class: 'glance-chip is-dotted-date',
    'aria-expanded': 'false',
    'aria-label': `${label}，${m.glanceHint || PENCIL_COPY[reason] || '点一下确认落墨'}`,
    text: label,
  });
  const run = async (fn, okText) => {
    try { whisper((await fn()) || okText, 2200); }
    catch (err) { haptic('warning'); whisper(err.message, 2800); }
  };
  chip.addEventListener('click', (e) => {
    e.stopPropagation();
    haptic('light');
    const existing = wrap.querySelector('.glance-actions');
    if (existing) { existing.remove(); chip.setAttribute('aria-expanded', 'false'); return; }
    chip.setAttribute('aria-expanded', 'true');
    const row = h('div', { class: 'glance-actions', onClick: (ev) => ev.stopPropagation() });
    if (reason === 'date_glance' && fact) {
      const picker = chineseDatePill(fact.triggerDate || todayIso(), '改成哪一天');
      row.append(
        h('button', { type: 'button', class: 'glance-btn', text: '确认落墨', onClick: () => run(() => confirmPencilMaterial(personId, m, null), '好，已经落墨') }),
        picker.el,
        h('button', { type: 'button', class: 'glance-btn quiet', text: '改成这天', onClick: () => run(() => confirmPencilMaterial(personId, m, picker.input.value), '改好了') }),
      );
    } else if (m.inboxItemId) {
      const res = (body, after, msg, hk = 'success') => run(async () => { await api.resolveInbox(m.inboxItemId, body); haptic(hk); after(); return msg; });
      row.append(
        h('button', { type: 'button', class: 'glance-btn', text: '确认落墨', onClick: () => res({ action: 'confirm' }, () => inked(personId, m), '好，已经落墨') }),
        h('button', { type: 'button', class: 'glance-btn quiet', text: '只当随手记', onClick: () => res({ action: 'keep_as_note' }, () => inked(personId, m), '留作一张随手记', 'light') }),
        h('button', { type: 'button', class: 'glance-btn quiet', text: '划掉', onClick: () => res({ action: 'discard' }, () => removeMaterial(personId, m.id), '划掉了', 'warning') }),
      );
    } else {
      row.appendChild(h('button', { type: 'button', class: 'glance-btn', text: '确认落墨', onClick: () => run(() => confirmPencilMaterial(personId, m, null), '好，已经落墨') }));
    }
    wrap.appendChild(row);
  });
  wrap.appendChild(chip);
  return wrap;
}

/** Question sticky note: up to 3 candidate avatars (from the open inbox item). */
function questionControls(m, personId, ctx) {
  const row = h('div', { class: 'candidate-row', 'aria-label': '可能是这几位', onClick: (e) => e.stopPropagation() });
  const fill = (item) => {
    clear(row);
    const candidates = (item?.candidatePersons || []).filter((c) => c.personId !== personId).slice(0, 3);
    for (const c of candidates) {
      row.appendChild(h('button', {
        type: 'button', class: 'candidate-btn', 'aria-label': `是${c.displayName}`,
        onClick: async () => {
          try {
            await api.resolveInbox(m.inboxItemId, { action: m.pencilReason === 'new_person' ? 'merge_person' : 'assign_person', targetPersonId: c.personId });
            haptic('success'); removeMaterial(personId, m.id); actions.reloadMaterials(c.personId); actions.refreshBootstrap(); whisper(`好，记到「${c.displayName}」那页了`, 2600);
          } catch (err) { haptic('warning'); whisper(err.message, 2800); }
        },
      }, h('span', { class: ['mono-seal', c.tier === 'silver' && 'is-cloaked'], style: c.tier === 'silver' ? {} : sealStyle({ id: c.personId, tier: c.tier }), text: c.monogram || monogramOf({ nameCn: c.displayName }) }), h('span', { text: c.displayName })));
    }
    const name = item?.mention || item?.personName || store.get().people[personId]?.nameCn || 'TA';
    const create = async (tier, okMsg) => {
      try {
        await api.resolveInbox(m.inboxItemId, { action: 'create_person', tier, displayName: name });
        haptic('success'); inked(personId, m); actions.refreshBootstrap(); actions.reloadMaterials(personId); whisper(okMsg, 2600);
      } catch (err) { haptic('warning'); whisper(err.message, 2800); }
    };
    if (m.pencilReason === 'new_person') {
      for (const [tier, label] of [['gold', '金箭 · 记爱'], ['silver', '银箭 · 记恨'], ['crystal', '水晶箭 · 记繁琐']]) {
        row.appendChild(h('button', { type: 'button', class: ['glance-btn', tier !== 'gold' && 'quiet'], text: label, onClick: () => create(tier, `好，「${name}」有了自己的一页`) }));
      }
      return;
    }
    row.appendChild(h('button', { type: 'button', class: 'glance-btn quiet', text: candidates.length ? '都不是，是新朋友' : '是新朋友', onClick: () => create('crystal', '好，记下这位新朋友了') }));
  };
  if (m.inboxItemId) ctx.inboxItem(m.inboxItemId, personId).then(fill).catch(() => fill(null));
  return row;
}

/* ---------------- iOS Peek Focus Overlay ---------------- */
export function closePeek({ silentHaptic = false } = {}) {
  if (!activePeek) return;
  const { layer, keyHandler, cleanupDrag, restore, rafId } = activePeek;
  activePeek = null;
  if (rafId) cancelAnimationFrame(rafId);
  cleanupDrag?.();
  if (!silentHaptic) haptic('light');
  layer.classList.remove('is-open');
  document.removeEventListener('keydown', keyHandler);
  syncPeekOverlayState();
  setTimeout(() => { if (!activePeek) clear(layer); }, 280);
  if (restore && typeof restore.focus === 'function' && restore.isConnected) restore.focus({ preventScroll: true });
}

export function openPeekOverlay(materialId, person) {
  const layer = $('#peekLayer');
  const fresh = () => store.get().materials[person?.id]?.items.find((x) => x.id === materialId) || null;
  const m = fresh();
  if (!layer || !person || !m) return;
  if (activePeek) closePeek({ silentHaptic: true });
  clear(layer);
  haptic('light');

  const tier = person.tier || 'crystal';
  const isPencil = m.presentation === 'pencil';
  const showPhoto = m.type === 'photo' && m.mediaUrl && tier !== 'silver';
  const fullDate = formatDateFullCn(m.happenedOn || m.createdAt) || formatDateCn(m.happenedOn || m.createdAt);
  let editing = false;
  let deleteArmed = false;

  const bodyHost = h('div', { class: 'peek-body-host' }, h('p', { class: 'peek-body', text: m.body || m.snippet || '' }));
  const grabberHit = h('div', { class: 'peek-grabber-hit', 'aria-hidden': 'true' }, h('div', { class: 'peek-grabber' }));
  const card = h(
    'div',
    { class: ['peek-card', isPencil && 'is-pencil'], role: 'dialog', 'aria-modal': 'true', tabindex: '-1', 'aria-label': '细看这一页手帐' },
    grabberHit,
    m.pinned ? h('span', { class: 'brass-pin is-peek', 'aria-hidden': 'true' }) : null,
    showPhoto ? h('div', { class: 'peek-photo-wrap' }, h('img', { src: m.mediaUrl, alt: '照片' })) : null,
    m.type === 'voice' ? h('div', { class: 'peek-voice-row' }, voiceCharm(m)) : null,
    bodyHost,
    fullDate ? h('p', { class: 'peek-date', text: fullDate }) : null,
  );

  if (isPencil) {
    const fact = primaryPencilFact(m);
    const picker = fact ? chineseDatePill(fact.triggerDate || m.happenedOn || todayIso(), '确认日期') : null;
    const confirmBtn = h('button', {
      type: 'button', class: 'peek-confirm-btn', text: '确认并落墨',
      onClick: async () => {
        confirmBtn.disabled = true;
        try {
          const msg = await confirmPencilMaterial(person.id, fresh() || m, picker ? picker.input.value : null);
          closePeek({ silentHaptic: true });
          whisper(msg, 2200);
        } catch (err) { confirmBtn.disabled = false; haptic('warning'); whisper(err.message, 2800); }
      },
    });
    card.appendChild(h('div', { class: 'peek-pencil-bar' }, picker?.el, confirmBtn));
  }

  const exitEdit = () => {
    editing = false;
    const cur = fresh() || m;
    clear(bodyHost);
    bodyHost.appendChild(h('p', { class: 'peek-body', text: cur.body || cur.snippet || '' }));
  };

  const enterEdit = () => {
    if (editing) return exitEdit();
    editing = true;
    haptic('light');
    const cur = fresh() || m;
    const ta = h('textarea', { class: 'peek-edit-area', rows: '4', maxlength: '10000', value: cur.body || cur.snippet || '', 'aria-label': '修改记录文字' });
    const saveBtn = h('button', {
      type: 'button', class: 'peek-confirm-btn', text: '保存修改',
      onClick: async () => {
        const nextText = ta.value.trim();
        if (!nextText) return whisper('写点什么再保存吧', 2000);
        saveBtn.disabled = true;
        try {
          const latest = fresh() || cur;
          const updated = await api.patchMaterial(latest.id, { body: nextText, snippet: nextText.slice(0, 60) }, latest.version);
          patchMaterial(person.id, latest.id, updated || { body: nextText, snippet: nextText.slice(0, 60) });
          haptic('success'); exitEdit(); whisper('改好了', 2000);
        } catch (err) { saveBtn.disabled = false; haptic('warning'); whisper(err.message, 2800); }
      },
    });
    clear(bodyHost);
    bodyHost.append(ta, h('div', { class: 'peek-edit-actions' }, h('button', { type: 'button', class: 'glance-btn quiet', text: '取消', onClick: exitEdit }), saveBtn));
    requestAnimationFrame(() => ta.focus({ preventScroll: true }));
  };

  const pinBtn = h('button', {
    type: 'button', class: 'peek-pill-btn', text: m.pinned ? '取下铜钉' : '钉在前面',
    onClick: async () => {
      pinBtn.disabled = true;
      try {
        const latest = fresh() || m;
        const nextPinned = !latest.pinned;
        const updated = await api.patchMaterial(latest.id, { pinned: nextPinned }, latest.version);
        patchMaterial(person.id, latest.id, updated || { pinned: nextPinned });
        haptic('medium'); closePeek({ silentHaptic: true }); whisper(nextPinned ? '已钉在前面' : '已取下铜钉', 2000);
      } catch (err) { pinBtn.disabled = false; haptic('warning'); whisper(err.message, 2800); }
    },
  });

  const deleteBtn = h('button', {
    type: 'button', class: 'peek-pill-btn is-danger', text: '撕掉',
    onClick: async () => {
      if (!deleteArmed) { deleteArmed = true; haptic('warning'); deleteBtn.textContent = '再点撕掉'; deleteBtn.classList.add('is-armed'); return; }
      deleteBtn.disabled = true;
      try {
        await api.deleteMaterial(m.id);
        haptic('warning'); removeMaterial(person.id, m.id); closePeek({ silentHaptic: true }); whisper('已轻轻撕下这一页', 2200);
      } catch (err) { deleteBtn.disabled = false; deleteArmed = false; deleteBtn.textContent = '撕掉'; deleteBtn.classList.remove('is-armed'); whisper(err.message, 2800); }
    },
  });

  const pill = h(
    'div',
    { class: 'peek-action-pill', role: 'toolbar', 'aria-label': '手帐操作' },
    pinBtn,
    h('button', { type: 'button', class: 'peek-pill-btn', text: '问丘比特', onClick: () => { haptic('light'); closePeek({ silentHaptic: true }); actions.openChat(person.id, { materialId: m.id }); } }),
    h('button', { type: 'button', class: 'peek-pill-btn', text: '改字', onClick: enterEdit }),
    deleteBtn,
  );
  const scrim = h('div', { class: 'peek-scrim', onClick: () => closePeek() });
  const stage = h('div', { class: 'peek-stage', onClick: (e) => { if (e.target === stage) closePeek(); } }, card, pill);
  layer.append(scrim, stage);

  const cleanupDrag = attachSwipeDismiss({
    container: card,
    scrim,
    scaleOnDrag: true,
    headerOnly: true,
    canStart: (e) => !editing && Boolean(e.target?.closest?.('.peek-grabber-hit, .peek-grabber')),
    onDismiss: () => closePeek({ silentHaptic: true }),
  });
  const keyHandler = (e) => {
    if (e.key === 'Escape') { e.preventDefault(); (editing ? exitEdit() : closePeek()); return; }
    if (e.key !== 'Tab') return;
    const nodes = Array.from(stage.querySelectorAll(FOCUSABLE)).filter((n) => n.offsetParent !== null);
    if (!nodes.length) { e.preventDefault(); card.focus({ preventScroll: true }); return; }
    const first = nodes[0], last = nodes[nodes.length - 1];
    if (e.shiftKey && (document.activeElement === first || !stage.contains(document.activeElement))) { e.preventDefault(); last.focus({ preventScroll: true }); }
    else if (!e.shiftKey && (document.activeElement === last || !stage.contains(document.activeElement))) { e.preventDefault(); first.focus({ preventScroll: true }); }
  };
  document.addEventListener('keydown', keyHandler);
  const entry = { layer, keyHandler, cleanupDrag, restore: document.activeElement, rafId: 0 };
  activePeek = entry;
  syncPeekOverlayState();
  entry.rafId = requestAnimationFrame(() => {
    if (activePeek !== entry) return;
    layer.classList.add('is-open');
    setTimeout(() => { if (activePeek === entry) (card.querySelector('button') || card).focus({ preventScroll: true }); }, 90);
  });
}

/* ---------------- Resting Scrapbook Slip Renderer ---------------- */
export function renderMaterial(m, person, ctx = {}) {
  const tier = person?.tier || 'crystal';
  const d = materialDecor(m, tier);
  const presentation = m.presentation || 'ink';
  const isPencil = presentation === 'pencil';
  const isQuestion = presentation === 'question' && m.inboxItemId;
  const showPhoto = m.type === 'photo' && m.mediaUrl && tier !== 'silver';
  const date = formatDateCn(m.happenedOn || m.createdAt);
  const body = m.body || m.snippet || '';
  const rot = ctx.rot != null ? ctx.rot : d.rotation;
  const side = ctx.side || d.side;

  const card = h('article', {
    class: ['mat-card', ctx.layout && `is-${ctx.layout}`, side && `is-${side}`, isPencil && 'is-pencil', m.pinned && !isQuestion && 'is-pinned'],
    dataset: { materialId: m.id, side },
    style: { '--rot': `${rot}deg` },
    role: isQuestion ? null : 'button',
    tabindex: isQuestion ? null : '0',
    'aria-label': `${body.slice(0, 40)}${date ? `，${date}` : ''}`,
  });
  card.addEventListener('animationend', () => card.classList.add('is-settled'), { once: true });

  const pinEl = m.pinned && !isQuestion ? h('span', { class: 'brass-pin', 'aria-hidden': 'true' }) : null;
  const dateEl = isPencil ? pencilFooter(m, person?.id, date) : date ? h('p', { class: 'mat-sign', text: date }) : null;
  let shell;
  if (isQuestion) {
    shell = h('div', { class: 'mat-shell mat-question' }, h('span', { class: 'question-badge', 'aria-hidden': 'true', text: '?' }), h('p', { class: 'question-ask', text: PENCIL_COPY[m.pencilReason] || '这是在说谁？' }), renderHighlightedBody(body, ctx.query), questionControls(m, person?.id, ctx));
  } else if (showPhoto) {
    shell = h('figure', { class: 'mat-shell mat-photo' }, pinEl || h('span', { class: 'photo-brass-clip', 'aria-hidden': 'true' }, svg(BRASS_CLIP_SVG)), h('div', { class: 'photo-slip-frame' }, h('img', { src: m.mediaUrl, alt: '手帐照片', loading: 'lazy', decoding: 'async' })), h('figcaption', { class: 'photo-slip-meta' }, body ? renderHighlightedBody(body, ctx.query) : null, dateEl));
  } else if (m.type === 'voice') {
    shell = h('div', { class: 'mat-shell mat-voice' }, pinEl, renderHighlightedBody(body, ctx.query), h('div', { class: 'voice-slip-foot' }, voiceCharm(m), isPencil ? pencilFooter(m, person?.id, date) : date ? h('span', { class: 'mat-sign', text: date }) : null));
  } else {
    shell = h('div', { class: 'mat-shell mat-note' }, pinEl || h('span', { class: 'mat-washi', 'aria-hidden': 'true', style: { background: d.tapeColor, '--tape-x': `${d.tapeShift}px` } }), renderHighlightedBody(body, ctx.query), dateEl);
  }

  if (!isQuestion) {
    card.addEventListener('click', (e) => { if (!e.target.closest('button, input, textarea, a')) openPeekOverlay(m.id, person); });
    card.addEventListener('keydown', (e) => { if (e.target === card && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); openPeekOverlay(m.id, person); } });
  }
  card.appendChild(shell);
  return card;
}

export function renderPendingSlip(p) {
  const failed = p.state === 'failed';
  const queued = p.state === 'queued';
  const label = failed ? '没能记下' : queued ? '本地草稿 · 联网后自动补传' : '墨水还没干…';
  const card = h(
    'article',
    { class: ['mat-card', 'is-medium', 'is-right', 'is-drying', failed && 'is-failed'], dataset: { pendingId: p.id, side: 'right' }, style: { '--rot': '1.1deg' }, 'aria-busy': String(!failed && !queued) },
    h('div', { class: 'mat-shell mat-note' }, h('span', { class: 'drying-label', text: label }), h('p', { class: 'mat-body', text: p.text || '' })),
  );
  card.addEventListener('animationend', () => card.classList.add('is-settled'), { once: true });
  return card;
}

/** Highlight a material on the current page; returns true if found. */
export function flashMaterial(materialId) {
  const el = document.querySelector(`[data-material-id="${CSS.escape(String(materialId))}"]`);
  if (!el) return false;
  const scroll = $('#journalScroll');
  if (scroll && scroll.contains(el)) {
    const r = el.getBoundingClientRect();
    const sr = scroll.getBoundingClientRect();
    scroll.scrollTo({ top: Math.max(0, r.top - sr.top + scroll.scrollTop - Math.max(88, (sr.height - r.height) / 2)), behavior: prefersReducedMotion() ? 'auto' : 'smooth' });
  }
  el.classList.remove('is-settled', 'is-flash');
  void el.offsetWidth;
  el.classList.add('is-flash');
  setTimeout(() => el.classList.remove('is-flash'), 1500);
  return true;
}

export function peopleCount() {
  const s = store.get();
  return s.coreIds.length + s.casualIds.length;
}
