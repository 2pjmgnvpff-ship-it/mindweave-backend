// journal-items.js — one renderer per material: note / voice / photo, D1 pencil drafts,
// question sticky notes and optimistic "drying ink" slips. All text via textContent.
import { h, clear } from '../dom.js';
import { materialDecor, monogramOf, sealStyle } from '../decor.js';
import { formatDateCn, todayIso } from '../format.js';
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

function voiceCharm(m) {
  const playable = Boolean(m.hasMedia && m.mediaUrl);
  const btn = h(
    'button',
    { type: 'button', class: 'voice-charm', disabled: !playable, 'aria-label': playable ? `播放语音 ${m.dur || ''}` : '语音' },
    h('span', { class: 'voice-charm-hole' }),
    h('span', { class: 'voice-charm-play', text: '▶' }),
    h('span', { class: 'voice-charm-bars' }, h('i'), h('i'), h('i'), h('i'), h('i'), h('i')),
    h('span', { text: m.dur || '' }),
  );
  if (playable) {
    btn.addEventListener('click', () => {
      if (playing && playing.btn === btn) {
        playing.audio.pause();
        return;
      }
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

function inked(personId, m) {
  patchMaterial(personId, m.id, { presentation: 'ink', glanceHint: null, pencilReason: null, pencilFacts: [], inboxItemId: null });
}

/** The tappable glanceHint chip → confirm / edit in place. */
function glanceControls(m, personId) {
  const wrap = h('div');
  const reason = m.pencilReason || '';
  const fact = (m.pencilFacts || []).find((f) => f.triggerDate) || (m.pencilFacts || [])[0] || null;
  const chip = h('button', {
    type: 'button',
    class: 'glance-chip',
    'aria-expanded': 'false',
    text: `✎ ${m.glanceHint || PENCIL_COPY[reason] || '铅笔草稿，点一下确认'}`,
  });
  const run = async (fn, okText) => {
    try {
      await fn();
      whisper(okText, 2200);
    } catch (err) {
      whisper(err.message, 2800);
    }
  };
  chip.addEventListener('click', () => {
    if (wrap.querySelector('.glance-actions')) return;
    chip.setAttribute('aria-expanded', 'true');
    const actionsRow = h('div', { class: 'glance-actions' });
    if (reason === 'date_glance' && fact) {
      const dateInput = h('input', { type: 'date', class: 'glance-date', value: fact.triggerDate || todayIso(), 'aria-label': '改成哪一天' });
      actionsRow.append(
        h('button', {
          type: 'button',
          class: 'glance-btn',
          text: '✔ 对的',
          onClick: () => run(async () => { await api.patchFact(fact.id, { status: 'confirmed' }); inked(personId, m); }, '好，已经落墨'),
        }),
        dateInput,
        h('button', {
          type: 'button',
          class: 'glance-btn quiet',
          text: '改成这天',
          onClick: () => run(async () => { await api.patchFact(fact.id, { triggerDate: dateInput.value }); inked(personId, m); }, `改好了，${formatDateCn(dateInput.value)}`),
        }),
      );
    } else if (m.inboxItemId) {
      const resolve = (body, after, text) => run(async () => { await api.resolveInbox(m.inboxItemId, body); after(); }, text);
      actionsRow.append(
        h('button', { type: 'button', class: 'glance-btn', text: '✔ 没错', onClick: () => resolve({ action: 'confirm' }, () => inked(personId, m), '好，已经落墨') }),
        h('button', { type: 'button', class: 'glance-btn quiet', text: '只当随手记', onClick: () => resolve({ action: 'keep_as_note' }, () => inked(personId, m), '留作一张随手记') }),
        h('button', { type: 'button', class: 'glance-btn quiet', text: '划掉', onClick: () => resolve({ action: 'discard' }, () => removeMaterial(personId, m.id), '划掉了') }),
      );
    } else {
      actionsRow.append(
        h('button', {
          type: 'button',
          class: 'glance-btn',
          text: '✔ 没错',
          onClick: () => run(async () => {
            for (const f of m.pencilFacts || []) await api.patchFact(f.id, { status: 'confirmed' });
            inked(personId, m);
          }, '好，已经落墨'),
        }),
      );
    }
    wrap.appendChild(actionsRow);
  });
  wrap.prepend(chip);
  return wrap;
}

/** Question sticky note: up to 3 candidate avatars (from the open inbox item). */
function questionControls(m, personId, ctx) {
  const row = h('div', { class: 'candidate-row', 'aria-label': '可能是这几位' });
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
                await api.resolveInbox(m.inboxItemId, { action: 'assign_person', targetPersonId: c.personId });
                removeMaterial(personId, m.id);
                actions.reloadMaterials(c.personId);
                actions.refreshBootstrap();
                whisper(`好，记到「${c.displayName}」那页了`, 2600);
              } catch (err) {
                whisper(err.message, 2800);
              }
            },
          },
          h('span', { class: 'mono-seal', style: sealStyle({ id: c.personId, tier: c.tier }), text: c.monogram || monogramOf({ nameCn: c.displayName }) }),
          h('span', { text: c.displayName }),
        ),
      );
    }
    row.appendChild(
      h('button', {
        type: 'button',
        class: 'glance-btn quiet',
        text: candidates.length ? '都不是，是新朋友' : '是新朋友',
        onClick: async () => {
          try {
            await api.resolveInbox(m.inboxItemId, { action: 'confirm' });
            inked(personId, m);
            actions.refreshBootstrap();
            whisper('好，记下这位新朋友了', 2600);
          } catch (err) {
            whisper(err.message, 2800);
          }
        },
      }),
    );
  };
  if (m.inboxItemId) {
    ctx.inboxItem(m.inboxItemId, personId).then(fill).catch(() => fill(null));
  }
  return row;
}

export function renderMaterial(m, person, ctx) {
  const tier = person?.tier || 'crystal';
  const d = materialDecor(m, tier);
  const presentation = m.presentation || 'ink';
  const isPencil = presentation === 'pencil';
  const isQuestion = presentation === 'question' && m.inboxItemId;
  const date = formatDateCn(m.happenedOn || m.createdAt);
  const card = h('article', {
    class: ['mat-card', isPencil && 'is-pencil'],
    dataset: { materialId: m.id, side: d.side },
    style: { '--rot': `${d.rotation}deg` },
    'aria-label': `${m.title || '一页手帐'}${date ? `，${date}` : ''}${isPencil ? '，铅笔草稿' : ''}`,
  });
  card.addEventListener('animationend', () => card.classList.add('is-settled'), { once: true });
  const body = m.body || m.snippet || '';
  let shell;
  if (isQuestion) {
    shell = h(
      'div',
      { class: 'mat-shell mat-question' },
      h('span', { class: 'question-badge', 'aria-hidden': 'true', text: '?' }),
      h('p', { class: 'question-ask', text: PENCIL_COPY[m.pencilReason] || '这是在说谁？' }),
      h('p', { class: 'mat-body', text: body }),
      questionControls(m, person?.id, ctx),
    );
  } else if (m.type === 'photo' && m.mediaUrl) {
    shell = h(
      'figure',
      { class: 'mat-shell mat-photo' },
      h('img', { src: m.mediaUrl, alt: m.title || '照片', loading: 'lazy', decoding: 'async' }),
      h('figcaption', { class: 'mat-sign', text: [m.title, date].filter(Boolean).join(' · ') }),
    );
  } else if (m.type === 'voice') {
    shell = h(
      'div',
      { class: 'mat-shell mat-voice' },
      h('div', { class: 'slip-top-meta' }, m.title ? h('span', { class: 'mat-title', text: m.title }) : h('span'), voiceCharm(m)),
      h('p', { class: 'mat-body', text: body }),
      date ? h('p', { class: 'mat-sign', text: date }) : null,
    );
  } else {
    shell = h(
      'div',
      { class: 'mat-shell mat-note' },
      h('span', { class: 'mat-washi', 'aria-hidden': 'true', style: { background: d.tapeColor, '--tape-x': `${d.tapeShift}px` } }),
      m.title ? h('span', { class: 'mat-title', text: m.title }) : null,
      h('p', { class: 'mat-body', text: body }),
      date ? h('p', { class: 'mat-sign', text: date }) : null,
    );
  }
  if (isPencil) {
    shell.prepend(h('span', { class: 'pencil-mark', text: '✎ 铅笔草稿' }));
    shell.appendChild(glanceControls(m, person?.id));
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
    { class: ['mat-card', 'is-drying', failed && 'is-failed'], dataset: { pendingId: p.id, side: 'right' }, style: { '--rot': '1.2deg' }, 'aria-busy': String(!failed && !queued) },
    h(
      'div',
      { class: 'mat-shell mat-note' },
      h('span', { class: 'drying-label', text: label }),
      h('p', { class: 'mat-body', text: p.text || '' }),
    ),
  );
  card.addEventListener('animationend', () => card.classList.add('is-settled'), { once: true });
  return card;
}

/** Highlight a material on the current page; returns true if found. */
export function flashMaterial(materialId) {
  const el = document.querySelector(`[data-material-id="${CSS.escape(String(materialId))}"]`);
  if (!el) return false;
  el.scrollIntoView({ behavior: 'smooth', block: 'center' });
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
