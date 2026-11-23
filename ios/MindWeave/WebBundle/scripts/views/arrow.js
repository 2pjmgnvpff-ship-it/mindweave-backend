// arrow.js — the top-right tactile arrow pin and its tier popover (金箭 记爱 / 银箭 记恨 / 水晶箭 记繁琐).
// Changing tier is a PATCH with a 5 s undo toast; crystal moves the person into the 记繁琐 directory.
import { h, clear, svg, $ } from '../dom.js';
import { api } from '../api/endpoints.js';
import { store, applyTierChange } from '../store.js';
import { ARROW_PIN_IMG, ARROW_SVGS } from '../icons.js';
import { TIER_LABEL } from '../format.js';
import { showToast, whisper } from './toast.js';
import { actions } from '../actions.js';
import { haptic } from '../native-bridge.js';

const ORDER = ['gold', 'silver', 'crystal'];
let popover = null;
let outsideHandler = null;

function closePopover() {
  if (!popover) return;
  popover.classList.remove('is-open');
  const btn = $('#arrowAnchor .pinned-arrow-tack-btn');
  if (btn) btn.setAttribute('aria-expanded', 'false');
  document.removeEventListener('pointerdown', outsideHandler, true);
  outsideHandler = null;
}

async function changeTier(person, tier, { silent = false } = {}) {
  const previous = person.tier;
  if (previous === tier) return;
  haptic('medium');
  applyTierChange({ personId: person.id, tier }); // optimistic
  try {
    const res = await api.setTier(person.id, tier);
    applyTierChange({ personId: person.id, tier: res.tier, coreIds: res.coreIds, casualIds: res.casualIds });
  } catch (err) {
    applyTierChange({ personId: person.id, tier: previous });
    haptic('warning');
    whisper(err.message, 3000);
    return;
  }
  if (tier === 'crystal' && store.get().activePersonId === person.id) {
    store.set({ lastDemotedId: person.id });
  }
  if (silent) return;
  showToast(`「${person.nameCn}」换成了${TIER_LABEL[tier].arrow} · ${TIER_LABEL[tier].short}`, {
    actionLabel: '撤销',
    ms: 5000,
    onAction: async () => {
      await changeTier({ ...person, tier }, previous, { silent: true });
      if (previous !== 'crystal') actions.openPerson(person.id);
      whisper(`已换回${TIER_LABEL[previous].arrow}`, 2200);
    },
  });
}

export function renderArrow(person) {
  const anchor = $('#arrowAnchor');
  clear(anchor);
  closePopover();
  popover = null;
  if (!person) return;
  const tier = person.tier || 'crystal';
  const btn = h(
    'button',
    {
      type: 'button',
      class: 'pinned-arrow-tack-btn',
      dataset: { tier },
      'aria-haspopup': 'true',
      'aria-expanded': 'false',
      'aria-label': `${TIER_LABEL[tier].arrow}，${TIER_LABEL[tier].short}。点一下换一支箭`,
    },
    h('span', { class: 'pinned-paper-chit', 'aria-hidden': 'true' }, h('span', { class: 'chit-inner-border' }), h('span', { class: 'chit-dog-ear' })),
    h('span', { class: 'arrow-shaft-cast-shadow', 'aria-hidden': 'true' }),
    h('img', { class: 'pinned-arrow-3d-img', src: ARROW_PIN_IMG[tier], alt: '', draggable: 'false' }),
  );
  popover = h(
    'div',
    { class: 'arrow-selector-popover', role: 'menu', 'aria-label': '换一支箭' },
    ...ORDER.map((t) =>
      h(
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
        h('span', { class: ['choice-icon-orb', t] }, svg(ARROW_SVGS[t])),
        h('span', { class: 'choice-copy' }, h('strong', { text: `${TIER_LABEL[t].arrow} · ${TIER_LABEL[t].short}` }), h('small', { text: TIER_LABEL[t].meaning })),
        h('span', { class: 'choice-check', 'aria-hidden': 'true', text: '✓' }),
      ),
    ),
  );
  btn.addEventListener('click', () => {
    const open = !popover.classList.contains('is-open');
    if (!open) return closePopover();
    haptic('light');
    popover.classList.add('is-open');
    btn.setAttribute('aria-expanded', 'true');
    outsideHandler = (e) => {
      if (!anchor.contains(e.target)) closePopover();
    };
    document.addEventListener('pointerdown', outsideHandler, true);
    popover.querySelector('.is-selected')?.focus({ preventScroll: true });
  });
  popover.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      closePopover();
      btn.focus();
    }
  });
  anchor.append(btn, popover);
}

/** Re-strike animation when the page changes. */
export function strikeArrow() {
  const btn = $('#arrowAnchor .pinned-arrow-tack-btn');
  if (!btn) return;
  btn.classList.remove('is-striking');
  void btn.offsetWidth;
  btn.classList.add('is-striking');
}
