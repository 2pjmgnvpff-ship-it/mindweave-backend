// dock.js — B1 + Judge Blocker #5 unified 52px 2-piece Gleb dock:
// [#capsule (44x44 recessed Hold-to-Speak Voice Well + flexible Active Person pill 妈妈 ▾)]
// + 10–12px + [52x52 Detached 5-Layer Living Cupid Orb] + zero-blur Inner-4 Porcelain Tray.
import { h, clear, svg, $ } from '../dom.js';
import { store } from '../store.js';
import { monogramOf, sealStyle } from '../decor.js';
import { MIC_SVG, MIC_HAIRLINE_SVG } from '../icons.js';
import { TIER_LABEL } from '../format.js';
import { actions } from '../actions.js';
import { initNativeBridge, haptic, hapticPrepare } from '../native-bridge.js';
import { createOrb, initOrbVisibilityGuard } from '../lib/orb.js';

const MAX_INNER = 4;
const WHISPER_SEEN_KEY = 'mw.dockWhisperSeen';

let trayOpen = false;
let trayEl = null;
let trayAc = null;
let orbMounted = null;
let lastKey = '';
let whisperEl = null;
let whisperTimer = 0;

function isSilver(p) {
  return (p?.tier || 'crystal') === 'silver';
}

function canShowAvatar(p) {
  return !isSilver(p) && Boolean(p?.avatarUrl);
}

function innerPeople(s) {
  const seen = new Set();
  const ordered = [];
  for (const id of [...(s.coreIds || []), ...(s.casualIds || [])]) {
    if (seen.has(id)) continue;
    const p = s.people[id];
    if (p) { seen.add(id); ordered.push(p); }
    if (ordered.length >= MAX_INNER) break;
  }
  return ordered;
}

function totalPeopleCount(s) {
  return Object.keys(s.people || {}).length;
}

function renderAvatarToken(person, cls) {
  const tier = person.tier || 'crystal';
  const silver = isSilver(person);
  const usePhoto = canShowAvatar(person);
  const el = h('span', {
    class: [cls, silver && 'is-cloaked'],
    dataset: { tier },
    style: usePhoto || silver ? {} : sealStyle(person),
  });
  if (usePhoto) el.appendChild(h('img', { src: person.avatarUrl, alt: '', draggable: 'false' }));
  else el.textContent = monogramOf(person);
  return el;
}

function dismissDockWhisper() {
  if (!whisperEl) return;
  clearTimeout(whisperTimer);
  whisperEl.classList.remove('is-visible');
  setTimeout(() => { whisperEl?.remove(); whisperEl = null; }, 220);
  try { localStorage.setItem(WHISPER_SEEN_KEY, '1'); } catch { /* ignore */ }
}

function maybeShowFirstVisitWhisper(dock) {
  if (!dock || whisperEl) return;
  try { if (localStorage.getItem(WHISPER_SEEN_KEY) === '1') return; } catch { /* ignore */ }
  whisperEl = h(
    'div',
    { class: 'dock-first-whisper', role: 'status', onClick: dismissDockWhisper },
    h('span', { text: '按住左侧话筒随口记，轻点右侧光珠聊丘比特' }),
  );
  dock.parentElement?.appendChild(whisperEl);
  requestAnimationFrame(() => whisperEl?.classList.add('is-visible'));
  whisperTimer = setTimeout(dismissDockWhisper, 6500);
}

function closeTray({ silentHaptic = false } = {}) {
  if (!trayOpen) return;
  trayOpen = false;
  trayAc?.abort();
  trayAc = null;
  if (!silentHaptic) haptic('light');
  if (trayEl) {
    trayEl.classList.remove('is-open');
    setTimeout(() => {
      if (!trayOpen && trayEl) trayEl.style.display = 'none';
    }, 220);
  }
  $('#capsule .capsule-person-btn')?.setAttribute('aria-expanded', 'false');
}

function buildTray(s) {
  const people = innerPeople(s);
  const total = totalPeopleCount(s);
  const hasPendingBadge = Boolean((s.draftCount || 0) > 0 || (s.inboxCount || 0) > 0 || (s.questionCount || 0) > 0);
  const activeId = s.activePersonId;

  const row1 = h(
    'div',
    { class: 'inner4-row-people', role: 'list', 'aria-label': '常记的人' },
    ...people.map((p) => {
      const selected = p.id === activeId;
      const tier = p.tier || 'crystal';
      return h(
        'button',
        {
          type: 'button',
          role: 'listitem',
          class: ['inner4-tile', selected && 'is-selected'],
          dataset: { personId: p.id, tier },
          'aria-pressed': String(selected),
          'aria-label': `${p.nameCn}（${TIER_LABEL?.[tier]?.short || ''}）`,
          onPointerdown: () => hapticPrepare('selection'),
          onClick: () => {
            haptic('selection');
            closeTray({ silentHaptic: true });
            actions.openPerson(p.id);
          },
        },
        renderAvatarToken(p, 'inner4-avatar'),
        h('span', { class: 'inner4-name', text: p.nameCn }),
      );
    }),
  );

  const row2 = h(
    'div',
    { class: 'inner4-row-actions' },
    h('button', {
      type: 'button',
      class: 'inner4-chip is-all',
      text: `全部人物 (${total}) ›`,
      onClick: () => {
        haptic('light');
        closeTray({ silentHaptic: true });
        actions.openPeopleSheet ? actions.openPeopleSheet() : actions.openDirectory?.();
      },
    }),
    h(
      'button',
      {
        type: 'button',
        class: 'inner4-chip is-archive',
        onClick: () => {
          haptic('light');
          closeTray({ silentHaptic: true });
          actions.openArchiveHistory?.();
        },
      },
      hasPendingBadge ? h('span', { class: 'mw-dot is-warm', 'aria-hidden': 'true' }) : null,
      h('span', { text: '最近归档' }),
    ),
    h('button', {
      type: 'button',
      class: 'inner4-chip is-create',
      text: '+ 新建',
      onClick: () => {
        haptic('light');
        closeTray({ silentHaptic: true });
        actions.openPeopleSheet?.({ mode: 'create' });
      },
    }),
  );

  return [row1, row2];
}

function openTray() {
  if (trayOpen) return closeTray();
  const dock = $('#dock');
  if (!dock) return;
  dismissDockWhisper();
  trayOpen = true;
  haptic('selection');

  if (!trayEl) {
    trayEl = $('#inner4Tray');
    if (!trayEl) {
      trayEl = h('div', {
        class: 'inner4-tray',
        id: 'inner4Tray',
        role: 'dialog',
        'aria-label': '快速切换人物与归档',
        style: { display: 'none' },
      });
      dock.parentElement?.appendChild(trayEl);
    }
  }
  clear(trayEl);
  trayEl.append(...buildTray(store.get()));
  trayEl.style.display = 'flex';
  requestAnimationFrame(() => trayEl?.classList.add('is-open'));
  $('#capsule .capsule-person-btn')?.setAttribute('aria-expanded', 'true');

  trayAc?.abort();
  trayAc = new AbortController();
  document.addEventListener('pointerdown', (e) => {
    if (!trayEl?.contains(e.target) && !$('#capsule')?.contains(e.target)) closeTray();
  }, { capture: true, signal: trayAc.signal });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeTray();
  }, { signal: trayAc.signal });
}

function cycleInnerPerson(dir) {
  const s = store.get();
  const list = innerPeople(s);
  if (list.length < 2) return;
  const idx = Math.max(0, list.findIndex((p) => p.id === s.activePersonId));
  const next = list[(idx + dir + list.length) % list.length];
  if (next && next.id !== s.activePersonId) {
    haptic('light', { intensity: 0.5 });
    actions.openPerson(next.id);
  }
}

function attachPersonFlick(personBtn) {
  let flick = null;
  personBtn.addEventListener('pointerdown', (e) => {
    hapticPrepare('selection');
    flick = { id: e.pointerId, x0: e.clientX, y0: e.clientY, swiped: false };
  }, { passive: true });
  personBtn.addEventListener('pointermove', (e) => {
    if (!flick || flick.id !== e.pointerId || flick.swiped) return;
    const dx = e.clientX - flick.x0;
    const dy = e.clientY - flick.y0;
    if (Math.abs(dx) > 36 && Math.abs(dx) > Math.abs(dy) * 1.4) {
      flick.swiped = true;
      cycleInnerPerson(dx < 0 ? 1 : -1);
    }
  }, { passive: true });
  personBtn.addEventListener('pointerup', (e) => {
    if (!flick || flick.id !== e.pointerId) return;
    const wasSwiped = flick.swiped;
    flick = null;
    if (wasSwiped) {
      personBtn.dataset.suppressClick = '1';
      setTimeout(() => { delete personBtn.dataset.suppressClick; }, 160);
    }
  }, { passive: true });
  personBtn.addEventListener('pointercancel', () => { flick = null; }, { passive: true });
}

function render(s) {
  const capsule = $('#capsule');
  if (!capsule) return;
  const active = s.people[s.activePersonId] || innerPeople(s)[0] || null;
  const total = totalPeopleCount(s);
  const badgeKey = `${s.draftCount || 0}:${s.inboxCount || 0}:${s.questionCount || 0}`;
  const key = [
    active?.id || '',
    active?.nameCn || '',
    active?.tier || '',
    canShowAvatar(active) ? active.avatarUrl : 'mono',
    total,
    badgeKey,
    (s.coreIds || []).slice(0, MAX_INNER).join(','),
  ].join('|');

  if (trayOpen && trayEl) {
    clear(trayEl);
    trayEl.append(...buildTray(s));
  }
  if (key === lastKey) return;
  lastKey = key;

  // Preserve the Voice Well element if already mounted inside #capsule so voice.js listeners stay bound!
  let voiceWell = capsule.querySelector('.capsule-voice-btn');
  if (!voiceWell) {
    voiceWell = h(
      'button',
      {
        type: 'button',
        class: 'capsule-voice-btn',
        id: 'capsuleVoiceBtn',
        'aria-label': '按住随口记一句',
      },
      svg(MIC_HAIRLINE_SVG || MIC_SVG, 'capsule-voice-icon'),
    );
  }

  const personBtn = h(
    'button',
    {
      type: 'button',
      class: 'capsule-person-btn',
      'aria-haspopup': 'dialog',
      'aria-expanded': String(trayOpen),
      'aria-label': active ? `正在看「${active.nameCn}」，轻点展开人物栏，左右轻滑切换` : '轻点新建或切换人物',
      onClick: () => {
        if (personBtn.dataset.suppressClick) return;
        if (!total) {
          actions.openPeopleSheet?.({ mode: 'create' });
          return;
        }
        openTray();
      },
    },
    active ? renderAvatarToken(active, 'capsule-mini-token') : null,
    h('span', { class: 'capsule-person-name', text: active ? active.nameCn : '添一位在意的人' }),
    h('span', { class: 'capsule-person-caret', 'aria-hidden': 'true', text: '▾' }),
  );
  attachPersonFlick(personBtn);

  capsule.classList.add('dock-capsule');
  capsule.classList.toggle('is-empty', !total);
  clear(capsule);
  capsule.append(voiceWell, personBtn);

  if (total > 0) maybeShowFirstVisitWhisper($('#dock'));
}

export function closeDockTray() {
  closeTray({ silentHaptic: true });
}

export function initDock() {
  initNativeBridge();
  initOrbVisibilityGuard?.($('#app') || document.documentElement);

  const dock = $('#dock');
  if (dock) dock.classList.add('mw-dock');
  const existingTray = $('#inner4Tray');
  if (existingTray) existingTray.style.display = 'none';

  // Hide any legacy detached left #voiceOrb if Lane C-1 kept the id for fallback; #capsuleVoiceBtn is the canonical Voice Well.
  const legacyOrb = $('#voiceOrb');
  if (legacyOrb && legacyOrb.parentElement === dock && !legacyOrb.closest('#capsule')) {
    legacyOrb.hidden = true;
  }

  const aiOrb = $('#aiOrb');
  if (aiOrb) {
    clear(aiOrb);
    orbMounted = createOrb({ size: 'dock', state: 'idle' });
    aiOrb.appendChild(orbMounted);
    aiOrb.setAttribute('aria-label', '轻点和丘比特聊聊');
    aiOrb.addEventListener('pointerdown', () => hapticPrepare('light'), { passive: true });
    aiOrb.addEventListener('click', () => {
      dismissDockWhisper();
      closeTray({ silentHaptic: true });
      haptic('light');
      actions.openChat(store.get().activePersonId || null);
    });
  }

  store.subscribe(render);
  render(store.get());
}
