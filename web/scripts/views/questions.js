// questions.js — "张便签等你看一眼": open who-is-this notes (new person / which one) in one sheet.
// Only question kinds (new_person_confirm, person_disambiguation) — pencil drafts stay in place on the page.
import { h, clear } from '../dom.js';
import { api } from '../api/endpoints.js';
import { monogramOf, sealStyle } from '../decor.js';
import { TIER_LABEL } from '../format.js';
import { openSheet, closeSheet } from './sheet.js';
import { whisper } from './toast.js';
import { actions } from '../actions.js';
import { haptic } from '../native-bridge.js';

const QUESTION_KINDS = new Set(['new_person_confirm', 'person_disambiguation']);

async function resolve(item, body, okText, list) {
  try {
    const res = await api.resolveInbox(item.id, body);
    haptic(body?.action === 'discard' ? 'warning' : 'success');
    whisper(okText, 2600);
    await actions.refreshBootstrap();
    if (item.personId) actions.reloadMaterials(item.personId);
    if (res?.targetPersonId && res.targetPersonId !== item.personId) actions.reloadMaterials(res.targetPersonId);
    list.remove(item.id);
  } catch (err) {
    haptic('warning');
    whisper(err.status === 409 ? '这张便签已经处理过了' : err.message, 2800);
    list.remove(item.id);
  }
}

function noteFor(item, list) {
  const name = item.mention || item.personName || 'TA';
  const actionsRow = h('div', { class: 'glance-actions' });
  if (item.kind === 'new_person_confirm') {
    for (const tier of ['gold', 'silver', 'crystal']) {
      actionsRow.appendChild(
        h('button', {
          type: 'button',
          class: ['glance-btn', tier !== 'gold' && 'quiet'],
          text: `${TIER_LABEL[tier].arrow} · ${TIER_LABEL[tier].short}`,
          onClick: () => resolve(item, { action: 'create_person', tier, displayName: name }, `好，「${name}」有了自己的一页`, list),
        }),
      );
    }
  } else {
    const candidates = (item.candidatePersons || []).slice(0, 3);
    for (const c of candidates) {
      actionsRow.appendChild(
        h(
          'button',
          {
            type: 'button',
            class: 'candidate-btn',
            'aria-label': `是${c.displayName}`,
            onClick: () => resolve(item, { action: 'assign_person', targetPersonId: c.personId }, `好，记到「${c.displayName}」那页了`, list),
          },
          h('span', { class: ['mono-seal', c.tier === 'silver' && 'is-cloaked'], style: c.tier === 'silver' ? {} : sealStyle({ id: c.personId, tier: c.tier }), text: c.monogram || monogramOf({ nameCn: c.displayName }) }),
          h('span', { text: c.displayName }),
        ),
      );
    }
    actionsRow.appendChild(
      h('button', {
        type: 'button',
        class: 'glance-btn quiet',
        text: '是新朋友',
        onClick: () => resolve(item, { action: 'create_person', tier: 'crystal', displayName: name }, `好，记下「${name}」了`, list),
      }),
    );
  }
  actionsRow.appendChild(h('button', { type: 'button', class: 'glance-btn quiet', text: '划掉', onClick: () => resolve(item, { action: 'discard' }, '划掉了', list) }));
  const ask = item.kind === 'new_person_confirm' ? `第一次提到「${name}」，要给 TA 一页吗？` : `说的是哪一位「${name}」？`;
  const el = h('div', { class: 'qnote', dataset: { itemId: item.id } }, h('p', { class: 'question-ask', text: ask }), h('p', { class: 'qnote-text', text: item.summary || '' }), actionsRow);
  return el;
}

export async function openQuestionNotes() {
  const body = h('div', { class: 'sheet-section' }, h('p', { class: 'sheet-sub', text: '正在取便签…' }));
  openSheet({ title: '等你看一眼的便签', sub: '丘比特拿不准的地方，替你先贴在这里。', body });
  let items = [];
  try {
    const res = await api.inbox({ limit: 30 });
    items = (res.items || []).filter((i) => QUESTION_KINDS.has(i.kind));
  } catch (err) {
    clear(body);
    body.appendChild(h('p', { class: 'sheet-sub', text: err.message }));
    return;
  }
  const list = {
    remove(id) {
      body.querySelector(`[data-item-id="${CSS.escape(id)}"]`)?.remove();
      if (!body.querySelector('.qnote')) {
        clear(body);
        body.appendChild(h('p', { class: 'sheet-sub', text: '都看完啦。' }));
        setTimeout(() => closeSheet(), 900);
      }
    },
  };
  clear(body);
  if (!items.length) {
    body.appendChild(h('p', { class: 'sheet-sub', text: '暂时没有要你看的便签。' }));
    actions.refreshBootstrap();
    return;
  }
  for (const item of items) body.appendChild(noteFor(item, list));
}
