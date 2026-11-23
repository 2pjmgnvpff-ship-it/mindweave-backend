// dock.js — centre capsule: who's page am I on, tap/scrub to switch between core people and 记繁琐.
// Silver (记恨) people strictly show a neutral monogram seal — never a photo — in all states.
import { h, clear, svg, $ } from '../dom.js';
import { store } from '../store.js';
import { monogramOf, sealStyle } from '../decor.js';
import { DIRECTORY_SVG, SPARK_SVG } from '../icons.js';
import { TIER_LABEL } from '../format.js';
import { actions } from '../actions.js';

const MAX_CORE = 6;
let expanded = false;
let scrub = null; // { pointerId, moved, wasExpanded, x, y, lastTarget }
let lastKey = '';
let scrubTipEl = null;

function isSilverCloaked(person) {
  return (person?.tier || 'crystal') === 'silver';
}

function canShowAvatarPhoto(person) {
  return !isSilverCloaked(person) && Boolean(person?.avatarUrl);
}

function face(person, cls) {
  const tier = person.tier || 'crystal';
  const cloaked = isSilverCloaked(person);
  const usePhoto = canShowAvatarPhoto(person);
  const el = h('span', {
    class: [cls, cloaked && 'is-cloaked'],
    style: usePhoto || cloaked ? {} : sealStyle(person),
    dataset: { tier },
  });
  if (usePhoto) el.appendChild(h('img', { src: person.avatarUrl, alt: '', draggable: 'false' }));
  else el.textContent = monogramOf(person);
  return el;
}

function collapsed(s) {
  const core = s.coreIds.map((id) => s.people[id]).filter(Boolean);
  const inDirectory = s.view === 'directory';
  const active = inDirectory ? null : s.people[s.activePersonId];
  const cluster = h('div', { class: ['capsule-portrait-cluster', inDirectory && 'is-casual-mode'] });
  if (inDirectory) {
    core.slice(0, 3).forEach((p, i) => cluster.appendChild(face(p, `cluster-mini-avatar core-slot-${i}`)));
  } else {
    const others = core.filter((p) => p.id !== active?.id);
    if (others[0]) cluster.appendChild(face(others[0], 'cluster-mini-avatar pos-left'));
    if (others[1]) cluster.appendChild(face(others[1], 'cluster-mini-avatar pos-right'));
    if (active) cluster.appendChild(face(active, 'capsule-active-token'));
  }
  cluster.appendChild(h('span', { class: ['cluster-mini-casual', inDirectory && 'is-active-casual'] }, svg(DIRECTORY_SVG)));
  return h('div', { class: 'capsule-collapsed-content', 'aria-hidden': 'true' }, cluster);
}

function expandedItems(s) {
  const core = s.coreIds.map((id) => s.people[id]).filter(Boolean).slice(0, MAX_CORE);
  const wrap = h('div', { class: 'capsule-expanded-items' });
  for (const p of core) {
    const tier = p.tier || 'crystal';
    const cloaked = isSilverCloaked(p);
    const usePhoto = canShowAvatarPhoto(p);
    const selected = s.view === 'journal' && s.activePersonId === p.id;
    const btn = h('button', {
      type: 'button',
      class: ['capsule-obj-avatar', selected && 'is-selected', cloaked && 'is-cloaked'],
      dataset: { tier, personId: p.id, scrubName: p.nameCn },
      style: usePhoto || cloaked ? {} : sealStyle(p),
      'aria-label': `${p.nameCn}（${TIER_LABEL[tier]?.short || ''}）`,
      'aria-pressed': String(selected),
      tabindex: expanded ? '0' : '-1',
    });
    if (usePhoto) btn.appendChild(h('img', { src: p.avatarUrl, alt: '', draggable: 'false' }));
    else btn.textContent = monogramOf(p);
    wrap.appendChild(btn);
  }
  wrap.appendChild(
    h(
      'button',
      {
        type: 'button',
        class: ['capsule-obj-avatar', 'is-casual-icon', s.view === 'directory' && 'is-selected'],
        dataset: { tier: 'crystal', directory: 'true', scrubName: '记繁琐' },
        'aria-label': `记繁琐名册（${s.casualIds.length} 位）`,
        'aria-pressed': String(s.view === 'directory'),
        tabindex: expanded ? '0' : '-1',
      },
      h('span', { class: 'casual-token-inner' }, svg(DIRECTORY_SVG)),
    ),
  );
  return { wrap, count: core.length + 1 };
}

function personKey(p) {
  return p ? `${p.tier}:${p.nameCn}:${isSilverCloaked(p) ? 'cloak' : p.avatarUrl || ''}` : '';
}

function updateScrubTip(capsule, target) {
  if (!scrubTipEl) return;
  const label = target?.dataset?.scrubName;
  if (!label || !expanded) {
    scrubTipEl.classList.remove('is-visible');
    return;
  }
  const capRect = capsule.getBoundingClientRect();
  const itemRect = target.getBoundingClientRect();
  const centerX = itemRect.left + itemRect.width / 2 - capRect.left;
  scrubTipEl.textContent = label;
  scrubTipEl.style.left = `${centerX}px`;
  scrubTipEl.classList.add('is-visible');
}

function clearScrubFocus(capsule) {
  capsule?.querySelectorAll('.is-scrub-focus').forEach((n) => n.classList.remove('is-scrub-focus'));
  scrubTipEl?.classList.remove('is-visible');
}

function render(s) {
  const capsule = $('#capsule');
  const active = s.people[s.activePersonId];
  const key = [expanded, s.view, s.activePersonId, personKey(active), s.coreIds.join(), s.casualIds.length, ...s.coreIds.map((id) => personKey(s.people[id]))].join('|');
  if (key === lastKey) return;
  lastKey = key;
  clear(capsule);
  const empty = !s.coreIds.length && !s.casualIds.length;
  capsule.classList.toggle('is-pure-landscape', empty);
  capsule.classList.toggle('is-expanded', expanded);
  const { wrap, count } = expandedItems(s);
  capsule.style.setProperty('--expanded-capsule-w', `${Math.max(150, count * 48 + 16)}px`);
  scrubTipEl = h('span', { class: 'capsule-scrub-tip', 'aria-hidden': 'true' });
  capsule.append(h('div', { class: 'capsule-landscape-bg', 'aria-hidden': 'true' }), h('div', { class: 'capsule-glass-sheen', 'aria-hidden': 'true' }), collapsed(s), wrap, scrubTipEl);
  capsule.setAttribute('aria-label', s.view === 'directory' ? '正在看记繁琐名册，点一下切换人物' : active ? `正在看「${active.nameCn}」，点一下切换人物` : '切换人物');
}

function setExpanded(on) {
  if (expanded === on) return;
  expanded = on;
  const capsule = $('#capsule');
  capsule.setAttribute('aria-expanded', String(on));
  render(store.get());
  if (on) {
    document.addEventListener('pointerdown', outside, true);
    $('#capsule .capsule-obj-avatar.is-selected')?.focus({ preventScroll: true });
  } else {
    document.removeEventListener('pointerdown', outside, true);
    clearScrubFocus(capsule);
  }
}

function outside(e) {
  if (!$('#capsule').contains(e.target)) setExpanded(false);
}

function choose(el) {
  if (!el) return;
  setExpanded(false);
  if (el.dataset.directory) actions.openDirectory();
  else if (el.dataset.personId) actions.openPerson(el.dataset.personId);
}

function itemAt(x, y) {
  const el = document.elementFromPoint(x, y);
  return el?.closest?.('.capsule-obj-avatar') || null;
}

export function initDock() {
  const capsule = $('#capsule');
  capsule.tabIndex = 0;
  capsule.setAttribute('role', 'group');
  capsule.setAttribute('aria-expanded', 'false');
  capsule.addEventListener('pointerdown', (e) => {
    const s = store.get();
    if (!s.coreIds.length && !s.casualIds.length) return;
    scrub = { pointerId: e.pointerId, moved: false, wasExpanded: expanded, x: e.clientX, y: e.clientY, lastTarget: null };
    if (!expanded) setExpanded(true);
    try {
      capsule.setPointerCapture(e.pointerId);
    } catch {
      /* ignore */
    }
  });
  capsule.addEventListener('pointermove', (e) => {
    if (!scrub || scrub.pointerId !== e.pointerId) return;
    if (Math.hypot(e.clientX - scrub.x, e.clientY - scrub.y) > 8) scrub.moved = true;
    const target = itemAt(e.clientX, e.clientY);
    capsule.querySelectorAll('.is-scrub-focus').forEach((n) => n !== target && n.classList.remove('is-scrub-focus'));
    target?.classList.add('is-scrub-focus');
    if (target !== scrub.lastTarget) {
      scrub.lastTarget = target;
      updateScrubTip(capsule, target);
      if (target && scrub.moved) {
        try {
          navigator.vibrate?.(5);
        } catch {
          /* ignore */
        }
      }
    }
  });
  capsule.addEventListener('pointerup', (e) => {
    if (!scrub || scrub.pointerId !== e.pointerId) return;
    const target = itemAt(e.clientX, e.clientY);
    const { moved, wasExpanded } = scrub;
    scrub = null;
    clearScrubFocus(capsule);
    if (target && (moved || wasExpanded)) choose(target);
  });
  capsule.addEventListener('pointercancel', () => {
    scrub = null;
    clearScrubFocus(capsule);
  });
  capsule.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      const hadInnerFocus = capsule.contains(document.activeElement);
      setExpanded(false);
      if (hadInnerFocus) capsule.focus({ preventScroll: true });
    } else if ((e.key === 'Enter' || e.key === ' ') && e.target === capsule) {
      e.preventDefault();
      setExpanded(!expanded);
    } else if ((e.key === 'Enter' || e.key === ' ') && e.target.closest?.('.capsule-obj-avatar')) {
      e.preventDefault();
      choose(e.target.closest('.capsule-obj-avatar'));
    }
  });
  const aiOrb = $('#aiOrb');
  aiOrb.append(svg(SPARK_SVG, 'ai-orb-spark-icon'));
  aiOrb.addEventListener('click', () => actions.openChat(store.get().view === 'journal' ? store.get().activePersonId : null));
  store.subscribe(render);
  render(store.get());
}
