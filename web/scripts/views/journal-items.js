// journal-items.js — pure tactile scrapbook slips (big serif text + tiny date only),
// D1 pencil drafts (dashed graphite + dotted date), question sticky notes,
// optimistic drying-ink slips, and tap-to-open iOS Peek Focus Overlay.
import { h, clear, svg, $ } from '../dom.js';
import { materialDecor, monogramOf, sealStyle } from '../decor.js';
import { formatDateCn, formatDateFullCn, todayIso, highlightParts } from '../format.js';
import { BRASS_CLIP_SVG } from '../icons.js';
import { api } from '../api/endpoints.js';
import { store, patchMaterial, removeMaterial } from '../store.js';
import { whisper } from './toast.js';
import { actions } from '../actions.js';

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

let playing = null;
let activePeek = null;

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
      if (playing && playing.btn === btn) return playing.audio.pause();
      if (playing) playing.audio.pause();
      const audio = new Audio(m.mediaUrl);
      playing = { audio, btn };
      btn.classList.add('is-playing');
      const done = () => {
        btn.classList.remove('is-playing');
        if (playing && playing.btn === btn) playing = null;
      };
      audio.addEventListener('pause', done);
      audio.addEventListener('ended', done);
      audio.play().catch(() => {
        done();
        whisper('这段语音暂时放不出来', 2400);
      });
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
    return changed ? `改好了，${formatDateCn(customDate)}` : '好，已经落墨';
  }
  if (m.inboxItemId) {
    await api.resolveInbox(m.inboxItemId, customDate ? { action: 'confirm', triggerDate: customDate } : { action: 'confirm' });
    inked(personId, m);
    return '好，已经落墨';
  }
  for (const f of m.pencilFacts || []) {
    const body = customDate && f.id === fact?.id && customDate !== f.triggerDate
      ? { triggerDate: customDate, status: 'confirmed' }
      : { status: 'confirmed' };
    await api.patchFact(f.id, body, f.version);
  }
  inked(personId, m);
  return '好，已经落墨';
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
    try {
      whisper((await fn()) || okText, 2200);
    } catch (err) {
      whisper(err.message, 2800);
    }
  };
  chip.addEventListener('click', (e) => {
    e.stopPropagation();
    const existing = wrap.querySelector('.glance-actions');
    if (existing) {
      existing.remove();
      chip.setAttribute('aria-expanded', 'false');
      return;
    }
    chip.setAttribute('aria-expanded', 'true');
    const row = h('div', { class: 'glance-actions', onClick: (ev) => ev.stopPropagation() });
    if (reason === 'date_glance' && fact) {
      const dateInput = h('input', { type: 'date', class: 'glance-date', value: fact.triggerDate || todayIso(), 'aria-label': '改成哪一天' });
      row.append(
        h('button', { type: 'button', class: 'glance-btn', text: '确认落墨', onClick: () => run(() => confirmPencilMaterial(personId, m, null), '好，已经落墨') }),
        dateInput,
        h('button', { type: 'button', class: 'glance-btn quiet', text: '改成这天', onClick: () => run(() => confirmPencilMaterial(personId, m, dateInput.value), '改好了') }),
      );
    } else if (m.inboxItemId) {
      const res = (body, after, msg) => run(async () => { await api.resolveInbox(m.inboxItemId, body); after(); return msg; });
      row.append(
        h('button', { type: 'button', class: 'glance-btn', text: '确认落墨', onClick: () => res({ action: 'confirm' }, () => inked(personId, m), '好，已经落墨') }),
        h('button', { type: 'button', class: 'glance-btn quiet', text: '只当随手记', onClick: () => res({ action: 'keep_as_note' }, () => inked(personId, m), '留作一张随手记') }),
        h('button', { type: 'button', class: 'glance-btn quiet', text: '划掉', onClick: () => res({ action: 'discard' }, () => removeMaterial(personId, m.id), '划掉了') }),
      );
    } else {
      row.append(h('button', { type: 'button', class: 'glance-btn', text: '确认落墨', onClick: () => run(() => confirmPencilMaterial(personId, m, null), '好，已经落墨') }));
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
      row.appendChild(
        h(
          'button',
          {
            type: 'button',
            class: 'candidate-btn',
            'aria-label': `是${c.displayName}`,
            onClick: async () => {
              try {
                const action = m.pencilReason === 'new_person' ? 'merge_person' : 'assign_person';
                await api.resolveInbox(m.inboxItemId, { action, targetPersonId: c.personId });
                removeMaterial(personId, m.id);
                actions.reloadMaterials(c.personId);
                actions.refreshBootstrap();
                whisper(`好，记到「${c.displayName}」那页了`, 2600);
              } catch (err) {
                whisper(err.message, 2800);
              }
            },
          },
          h('span', { class: ['mono-seal', c.tier === 'silver' && 'is-cloaked'], style: c.tier === 'silver' ? {} : sealStyle({ id: c.personId, tier: c.tier }), text: c.monogram || monogramOf({ nameCn: c.displayName }) }),
          h('span', { text: c.displayName }),
        ),
      );
    }
    const after = (text) => {
      inked(personId, m);
      actions.refreshBootstrap();
      actions.reloadMaterials(personId);
      whisper(text, 2600);
    };
    const name = item?.mention || item?.personName || store.get().people[personId]?.nameCn || 'TA';
    const create = async (tier, okMsg) => {
      try {
        await api.resolveInbox(m.inboxItemId, { action: 'create_person', tier, displayName: name });
        after(okMsg);
      } catch (err) {
        whisper(err.message, 2800);
      }
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
export function closePeek() {
  if (!activePeek) return;
  const { layer, keyHandler, restore, rafId } = activePeek;
  activePeek = null;
  if (rafId) cancelAnimationFrame(rafId);
  layer.classList.remove('is-open');
  document.removeEventListener('keydown', keyHandler);
  setTimeout(() => { if (!activePeek) clear(layer); }, 280);
  if (restore && typeof restore.focus === 'function') restore.focus({ preventScroll: true });
}

export function openPeekOverlay(materialId, person) {
  const layer = $('#peekLayer');
  const fresh = () => store.get().materials[person?.id]?.items.find((x) => x.id === materialId) || null;
  const m = fresh();
  if (!layer || !person || !m) return;
  if (activePeek) closePeek();
  clear(layer);

  const tier = person.tier || 'crystal';
  const isPencil = m.presentation === 'pencil';
  const showPhoto = m.type === 'photo' && m.mediaUrl && tier !== 'silver';
  const fullDate = formatDateFullCn(m.happenedOn || m.createdAt) || formatDateCn(m.happenedOn || m.createdAt);
  let editing = false;
  let deleteArmed = false;

  const bodyHost = h('div', { class: 'peek-body-host' }, h('p', { class: 'peek-body', text: m.body || m.snippet || '' }));
  const card = h(
    'div',
    { class: ['peek-card', isPencil && 'is-pencil'], role: 'dialog', 'aria-modal': 'true', 'aria-label': '细看这一页手帐' },
    m.pinned ? h('span', { class: 'brass-pin is-peek', 'aria-hidden': 'true' }) : null,
    showPhoto ? h('div', { class: 'peek-photo-wrap' }, h('img', { src: m.mediaUrl, alt: '照片' })) : null,
    m.type === 'voice' ? h('div', { class: 'peek-voice-row' }, voiceCharm(m)) : null,
    bodyHost,
    fullDate ? h('p', { class: 'peek-date', text: fullDate }) : null,
  );

  if (isPencil) {
    const fact = primaryPencilFact(m);
    const dateInput = h('input', { type: 'date', class: 'peek-date-input', value: fact?.triggerDate || m.happenedOn || todayIso(), 'aria-label': '确认日期' });
    const confirmBtn = h('button', {
      type: 'button',
      class: 'peek-confirm-btn',
      text: '确认并落墨',
      onClick: async () => {
        confirmBtn.disabled = true;
        try {
          const msg = await confirmPencilMaterial(person.id, fresh() || m, fact ? dateInput.value : null);
          closePeek();
          whisper(msg, 2200);
        } catch (err) {
          confirmBtn.disabled = false;
          whisper(err.message, 2800);
        }
      },
    });
    card.appendChild(h('div', { class: 'peek-pencil-bar' }, fact ? dateInput : null, confirmBtn));
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
    const cur = fresh() || m;
    const ta = h('textarea', { class: 'peek-edit-area', rows: '4', maxlength: '10000', value: cur.body || cur.snippet || '', 'aria-label': '修改记录文字' });
    const saveBtn = h('button', {
      type: 'button',
      class: 'peek-confirm-btn',
      text: '保存修改',
      onClick: async () => {
        const nextText = ta.value.trim();
        if (!nextText) return whisper('写点什么再保存吧', 2000);
        saveBtn.disabled = true;
        try {
          const latest = fresh() || cur;
          const updated = await api.patchMaterial(latest.id, { body: nextText, snippet: nextText.slice(0, 60) }, latest.version);
          patchMaterial(person.id, latest.id, updated || { body: nextText, snippet: nextText.slice(0, 60) });
          exitEdit();
          whisper('改好了', 2000);
        } catch (err) {
          saveBtn.disabled = false;
          whisper(err.message, 2800);
        }
      },
    });
    clear(bodyHost);
    bodyHost.append(ta, h('div', { class: 'peek-edit-actions' }, h('button', { type: 'button', class: 'glance-btn quiet', text: '取消', onClick: exitEdit }), saveBtn));
    requestAnimationFrame(() => ta.focus({ preventScroll: true }));
  };

  const pinBtn = h('button', {
    type: 'button',
    class: 'peek-pill-btn',
    text: m.pinned ? '取下铜钉' : '钉在前面',
    onClick: async () => {
      pinBtn.disabled = true;
      try {
        const latest = fresh() || m;
        const nextPinned = !latest.pinned;
        const updated = await api.patchMaterial(latest.id, { pinned: nextPinned }, latest.version);
        patchMaterial(person.id, latest.id, updated || { pinned: nextPinned });
        closePeek();
        whisper(nextPinned ? '已钉在前面' : '已取下铜钉', 2000);
      } catch (err) {
        pinBtn.disabled = false;
        whisper(err.message, 2800);
      }
    },
  });

  const deleteBtn = h('button', {
    type: 'button',
    class: 'peek-pill-btn is-danger',
    text: '撕掉',
    onClick: async () => {
      if (!deleteArmed) {
        deleteArmed = true;
        deleteBtn.textContent = '再点撕掉';
        deleteBtn.classList.add('is-armed');
        return;
      }
      deleteBtn.disabled = true;
      try {
        await api.deleteMaterial(m.id);
        removeMaterial(person.id, m.id);
        closePeek();
        whisper('已轻轻撕下这一页', 2200);
      } catch (err) {
        deleteBtn.disabled = false;
        deleteArmed = false;
        deleteBtn.textContent = '撕掉';
        deleteBtn.classList.remove('is-armed');
        whisper(err.message, 2800);
      }
    },
  });

  const pill = h(
    'div',
    { class: 'peek-action-pill', role: 'toolbar', 'aria-label': '手帐操作' },
    pinBtn,
    h('button', { type: 'button', class: 'peek-pill-btn', text: '问丘比特', onClick: () => { closePeek(); actions.openChat(person.id, { materialId: m.id }); } }),
    h('button', { type: 'button', class: 'peek-pill-btn', text: '改字', onClick: enterEdit }),
    deleteBtn,
  );
  const stage = h('div', { class: 'peek-stage', onClick: (e) => { if (e.target === stage) closePeek(); } }, card, pill);
  layer.append(h('div', { class: 'peek-scrim', onClick: () => closePeek() }), stage);

  const keyHandler = (e) => { if (e.key === 'Escape') (editing ? exitEdit() : closePeek()); };
  document.addEventListener('keydown', keyHandler);
  const entry = { layer, keyHandler, restore: document.activeElement, rafId: 0 };
  activePeek = entry;
  entry.rafId = requestAnimationFrame(() => { if (activePeek === entry) layer.classList.add('is-open'); });
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
    tabindex: isQuestion ? null : '0',
    'aria-label': `${body.slice(0, 40)}${date ? `，${date}` : ''}`,
  });
  card.addEventListener('animationend', () => card.classList.add('is-settled'), { once: true });

  const pinEl = m.pinned && !isQuestion ? h('span', { class: 'brass-pin', 'aria-hidden': 'true' }) : null;
  const dateEl = isPencil ? pencilFooter(m, person?.id, date) : date ? h('p', { class: 'mat-sign', text: date }) : null;
  let shell;
  if (isQuestion) {
    shell = h(
      'div',
      { class: 'mat-shell mat-question' },
      h('span', { class: 'question-badge', 'aria-hidden': 'true', text: '?' }),
      h('p', { class: 'question-ask', text: PENCIL_COPY[m.pencilReason] || '这是在说谁？' }),
      renderHighlightedBody(body, ctx.query),
      questionControls(m, person?.id, ctx),
    );
  } else if (showPhoto) {
    shell = h(
      'figure',
      { class: 'mat-shell mat-photo' },
      pinEl || h('span', { class: 'photo-brass-clip', 'aria-hidden': 'true' }, svg(BRASS_CLIP_SVG)),
      h('div', { class: 'photo-slip-frame' }, h('img', { src: m.mediaUrl, alt: '手帐照片', loading: 'lazy', decoding: 'async' })),
      h('figcaption', { class: 'photo-slip-meta' }, body ? renderHighlightedBody(body, ctx.query) : null, dateEl),
    );
  } else if (m.type === 'voice') {
    shell = h(
      'div',
      { class: 'mat-shell mat-voice' },
      pinEl,
      renderHighlightedBody(body, ctx.query),
      h('div', { class: 'voice-slip-foot' }, voiceCharm(m), isPencil ? pencilFooter(m, person?.id, date) : date ? h('span', { class: 'mat-sign', text: date }) : null),
    );
  } else {
    shell = h(
      'div',
      { class: 'mat-shell mat-note' },
      pinEl || h('span', { class: 'mat-washi', 'aria-hidden': 'true', style: { background: d.tapeColor, '--tape-x': `${d.tapeShift}px` } }),
      renderHighlightedBody(body, ctx.query),
      dateEl,
    );
  }

  if (!isQuestion) {
    card.addEventListener('click', (e) => { if (!e.target.closest('button, input, textarea, a')) openPeekOverlay(m.id, person); });
    card.addEventListener('keydown', (e) => {
      if (e.target === card && (e.key === 'Enter' || e.key === ' ')) {
        e.preventDefault();
        openPeekOverlay(m.id, person);
      }
    });
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
    const target = r.top - sr.top + scroll.scrollTop - Math.max(88, (sr.height - r.height) / 2);
    scroll.scrollTo({ top: Math.max(0, target), behavior: 'smooth' });
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
