// arrow.js — top-right minimalist tier emblem & popover (金箭 · 珍重 / 银盾 · 边界 / 水晶 · 日常).
// Uses scoped AbortController (02d LK-7) and Safari 26 display:none close discipline.
import { h, clear, svg, $ } from '../dom.js';
import { api } from '../api/endpoints.js';
import { store, applyTierChange } from '../store.js';
import { ARROW_SVGS } from '../icons.js';
import { TIER_LABEL } from '../format.js';
import { showUndoToast, whisper } from './toast.js';
import { actions } from '../actions.js';
import { haptic, hapticPrepare } from '../native-bridge.js';

const ORDER = ['gold', 'silver', 'crystal'];
const FALLBACK_LABELS = {
  gold: { arrow: '金箭', short: '珍重', meaning: '最珍视的人，丘比特会多替你想一步' },
  silver: { arrow: '银盾', short: '边界', meaning: '需要守好边界的人，照片默认收起' },
  crystal: { arrow: '水晶', short: '日常', meaning: '日常往来的人，礼数周全不耗神' },
};

let renderAc = null;
let popAc = null;
let popover = null;

function labelOf(tier) {
  return TIER_LABEL?.[tier] || FALLBACK_LABELS[tier] || FALLBACK_LABELS.crystal;
}

function closePopover() {
  if (!popover) return;
  popover.classList.remove('is-open');
  popover.style.display = 'none';
  const btn = $('#arrowAnchor .pinned-arrow-tack-btn');
  if (btn) btn.setAttribute('aria-expanded', 'false');
  popAc?.abort();
  popAc = null;
}

async function changeTier(person, tier, { silent = false } = {}) {
  const previous = person.tier || 'crystal';
  if (previous === tier) return;
  haptic('medium');
  applyTierChange({ personId: person.id, tier });
  try {
    const res = await api.setTier(person.id, tier);
    applyTierChange({ personId: person.id, tier: res.tier, coreIds: res.coreIds, casualIds: res.casualIds });
  } catch (err) {
    applyTierChange({ personId: person.id, tier: previous });
    haptic('warning');
    whisper(err?.message || '关系分类未能更新', 3000);
    return;
  }
  if (tier === 'crystal' && store.get().activePersonId === person.id) {
    store.set({ lastDemotedId: person.id });
  }
  if (silent) return;
  const lbl = labelOf(tier);
  showUndoToast(`「${person.nameCn}」已换为${lbl.arrow} · ${lbl.short}`, async () => {
    await changeTier({ ...person, tier }, previous, { silent: true });
    actions.openPerson(person.id);
    whisper(`已换回${labelOf(previous).arrow} · ${labelOf(previous).short}`, 2200);
  });
}

export function renderArrow(person) {
  const anchor = $('#arrowAnchor');
  if (!anchor) return;
  closePopover();
  renderAc?.abort();
  renderAc = new AbortController();
  clear(anchor);
  popover = null;
  if (!person) return;

  const tier = person.tier || 'crystal';
  const lbl = labelOf(tier);

  const btn = h(
    'button',
    {
      type: 'button',
      class: 'pinned-arrow-tack-btn',
      dataset: { tier },
      'aria-haspopup': 'menu',
      'aria-expanded': 'false',
      'aria-label': `${lbl.arrow} · ${lbl.short}，轻点切换关系分类`,
    },
    h('span', { class: ['tier-emblem-glyph', `is-${tier}`], 'aria-hidden': 'true' }, svg(ARROW_SVGS[tier] || ARROW_SVGS.crystal)),
  );

  popover = h(
    'div',
    { class: 'arrow-selector-popover', role: 'menu', 'aria-label': '切换关系分类', style: { display: 'none' } },
    ...ORDER.map((t) => {
      const meta = labelOf(t);
      return h(
        'button',
        {
          type: 'button',
          class: ['arrow-choice-item', t === tier && 'is-selected'],
          role: 'menuitemradio',
          'aria-checked': String(t === tier),
          onClick: () => {
            closePopover();
            changeTier(store.get().people[person.id] || person, t);
          },
        },
        h('span', { class: ['choice-icon-orb', t] }, svg(ARROW_SVGS[t] || ARROW_SVGS.crystal)),
        h('span', { class: 'choice-copy' }, h('strong', { text: `${meta.arrow} · ${meta.short}` }), h('small', { text: meta.meaning })),
        h('span', { class: 'choice-check', 'aria-hidden': 'true', text: t === tier ? '✓' : '' }),
      );
    }),
  );

  btn.addEventListener('pointerdown', () => hapticPrepare('selection'), { passive: true, signal: renderAc.signal });
  btn.addEventListener('click', () => {
    const isOpen = popover.classList.contains('is-open');
    if (isOpen) return closePopover();
    haptic('light');
    popover.style.display = 'flex';
    requestAnimationFrame(() => popover?.classList.add('is-open'));
    btn.setAttribute('aria-expanded', 'true');
    popAc?.abort();
    popAc = new AbortController();
    document.addEventListener('pointerdown', (e) => {
      if (!anchor.contains(e.target)) closePopover();
    }, { capture: true, signal: popAc.signal });
    popover.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { closePopover(); btn.focus({ preventScroll: true }); }
    }, { signal: popAc.signal });
    popover.querySelector('.is-selected')?.focus({ preventScroll: true });
  }, { signal: renderAc.signal });

  anchor.append(btn, popover);
}

/** Subtle arrival pulse when switching people. */
export function strikeArrow() {
  const btn = $('#arrowAnchor .pinned-arrow-tack-btn');
  if (!btn) return;
  btn.classList.remove('is-striking');
  void btn.offsetWidth;
  btn.classList.add('is-striking');
}
