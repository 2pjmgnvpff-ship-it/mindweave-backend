// dock.js — centre capsule: who's page am I on, tap/scrub to switch between core people and 记繁琐.
// Silver (记恨) people only ever show a neutral monogram seal — never a photo.
import { h, clear, svg, $ } from '../dom.js';
import { store } from '../store.js';
import { monogramOf, sealStyle } from '../decor.js';
import { DIRECTORY_SVG } from '../icons.js';
import { TIER_LABEL } from '../format.js';
import { actions } from '../actions.js';

const MAX_CORE = 6;
let expanded = false;
let scrub = null; // { pointerId, moved }
let lastKey = '';

function face(person, cls) {
  const tier = person.tier || 'crystal';
  const usePhoto = tier !== 'silver' && person.avatarUrl;
  const el = h('span', {
    class: [cls, tier === 'silver' && 'is-cloaked'],
    style: usePhoto || tier === 'silver' ? {} : sealStyle(person),
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
    const selected = s.view === 'journal' && s.activePersonId === p.id;
    const btn = h('button', {
      type: 'button',
      class: ['capsule-obj-avatar', selected && 'is-selected', p.tier === 'silver' && 'is-cloaked'],
      dataset: { tier: p.tier, personId: p.id },
      style: p.tier === 'silver' || (p.avatarUrl && p.tier !== 'silver') ? {} : sealStyle(p),
      'aria-label': `${p.nameCn}（${TIER_LABEL[p.tier]?.short || ''}）`,
      'aria-pressed': String(selected),
      tabindex: expanded ? '0' : '-1',
    });
    if (p.tier !== 'silver' && p.avatarUrl) btn.appendChild(h('img', { src: p.avatarUrl, alt: '', draggable: 'false' }));
    else btn.textContent = monogramOf(p);
    wrap.appendChild(btn);
  }
  wrap.appendChild(
    h(
      'button',
      {
        type: 'button',
        class: ['capsule-obj-avatar', 'is-casual-icon', s.view === 'directory' && 'is-selected'],
        dataset: { tier: 'crystal', directory: 'true' },
        'aria-label': `记繁琐名册（${s.casualIds.length} 位）`,
        'aria-pressed': String(s.view === 'directory'),
        tabindex: expanded ? '0' : '-1',
      },
      h('span', { class: 'casual-token-inner' }, svg(DIRECTORY_SVG)),
    ),
  );
  return { wrap, count: core.length + 1 };
}

function render(s) {
  const capsule = $('#capsule');
  const key = [expanded, s.view, s.activePersonId, s.coreIds.join(), s.casualIds.length, ...s.coreIds.map((id) => `${s.people[id]?.tier}${s.people[id]?.nameCn}`)].join('|');
  if (key === lastKey) return;
  lastKey = key;
  clear(capsule);
  const empty = !s.coreIds.length && !s.casualIds.length;
  capsule.classList.toggle('is-pure-landscape', empty);
  capsule.classList.toggle('is-expanded', expanded);
  const { wrap, count } = expandedItems(s);
  capsule.style.setProperty('--expanded-capsule-w', `${Math.max(150, count * 48 + 16)}px`);
  capsule.append(h('div', { class: 'capsule-landscape-bg', 'aria-hidden': 'true' }), h('div', { class: 'capsule-glass-sheen', 'aria-hidden': 'true' }), collapsed(s), wrap);
  const active = s.people[s.activePersonId];
  capsule.setAttribute('aria-label', s.view === 'directory' ? '正在看记繁琐名册，点一下切换人物' : active ? `正在看「${active.nameCn}」，点一下切换人物` : '切换人物');
}

function setExpanded(on) {
  if (expanded === on) return;
  expanded = on;
  $('#capsule').setAttribute('aria-expanded', String(on));
  render(store.get());
  if (on) {
    document.addEventListener('pointerdown', outside, true);
    $('#capsule .capsule-obj-avatar.is-selected')?.focus({ preventScroll: true });
  } else {
    document.removeEventListener('pointerdown', outside, true);
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
    scrub = { pointerId: e.pointerId, moved: false, wasExpanded: expanded, x: e.clientX, y: e.clientY };
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
  });
  capsule.addEventListener('pointerup', (e) => {
    if (!scrub || scrub.pointerId !== e.pointerId) return;
    const target = itemAt(e.clientX, e.clientY);
    const { moved, wasExpanded } = scrub;
    scrub = null;
    if (target && (moved || wasExpanded)) choose(target);
  });
  capsule.addEventListener('pointercancel', () => (scrub = null));
  capsule.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') setExpanded(false);
    else if ((e.key === 'Enter' || e.key === ' ') && e.target === capsule) {
      e.preventDefault();
      setExpanded(!expanded);
    } else if ((e.key === 'Enter' || e.key === ' ') && e.target.closest?.('.capsule-obj-avatar')) {
      e.preventDefault();
      choose(e.target.closest('.capsule-obj-avatar'));
    }
  });
  $('#aiOrb').addEventListener('click', () => actions.openChat(store.get().view === 'journal' ? store.get().activePersonId : null));
  store.subscribe(render);
  render(store.get());
}
