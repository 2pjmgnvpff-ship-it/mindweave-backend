// journal-scrubber.js — Whisper-quiet Ghost Temporal Spine (05 §5.2 & 03e Lane C-2).
// Reuses static #ghostSpine from index.html (0 duplicate IDs), mounts ONLY when totalSlips >= 25
// or months.length >= 3, auto-fades after 1,200ms, uses GPU translate3d, AbortController cancel,
// hapticPrepare('porcelainTick') on pointerdown, porcelainTick (>=45ms throttle) & yearDetent.
import { h, clear, $ } from './dom.js';
import { haptic, hapticPrepare } from './native-bridge.js';

const IDLE_FADE_MS = 1200;
const TICK_THROTTLE_MS = 45;

export function createGhostScrubber({ hostEl, onJumpMonth, onBackToLatest }) {
  const ac = new AbortController();
  let months = [];
  let activeYm = '';
  let enabled = false;
  let dragging = false;
  let idleTimer = 0;
  let lastTickAt = 0;
  let lastHapticYm = '';
  let lastHapticYear = null;
  let jumpAc = null;
  let cachedRect = null;

  const trackEl = h('div', { class: 'ghost-spine-track', 'aria-hidden': 'true' });
  const pillEl = h('div', { class: 'ghost-spine-pill', 'aria-hidden': 'true' });
  const latestBtn = h('button', {
    type: 'button',
    class: 'ghost-latest-pill',
    text: '回到最新 ↑',
    'aria-label': '回到最新记录',
    style: { display: 'none' },
    onClick: () => { haptic('porcelainTick'); onBackToLatest?.(); },
  });

  // Reuse static #ghostSpine in index.html to avoid duplicate DOM IDs.
  let spineEl = $('#ghostSpine');
  if (!spineEl) {
    spineEl = h('div', { class: 'ghost-spine', id: 'ghostSpine' });
    hostEl?.appendChild(spineEl);
  }
  clear(spineEl);
  spineEl.hidden = false;
  spineEl.setAttribute('role', 'slider');
  spineEl.setAttribute('aria-label', '时间轴快速定位');
  spineEl.setAttribute('aria-orientation', 'vertical');
  spineEl.style.display = 'none';
  spineEl.append(trackEl, pillEl);
  hostEl?.appendChild(latestBtn);

  function showBriefly() {
    if (!enabled) return;
    spineEl.classList.add('is-awake');
    clearTimeout(idleTimer);
    if (!dragging) {
      idleTimer = setTimeout(() => { if (!dragging) spineEl.classList.remove('is-awake'); }, IDLE_FADE_MS);
    }
  }

  function setPillPosition(idx) {
    if (!months.length) return;
    const clamped = Math.max(0, Math.min(months.length - 1, idx));
    const item = months[clamped];
    const ratio = months.length <= 1 ? 0 : clamped / (months.length - 1);
    const trackH = cachedRect ? Math.max(80, cachedRect.height - 28) : 260;
    pillEl.style.transform = `translate3d(0, ${Math.round(ratio * trackH)}px, 0)`;
    pillEl.textContent = item.label || item.ym;
    spineEl.setAttribute('aria-valuetext', item.label || item.ym);
  }

  function triggerIndex(idx, { fromScrub = false } = {}) {
    if (!months.length) return;
    const clamped = Math.max(0, Math.min(months.length - 1, idx));
    const target = months[clamped];
    if (!target) return;
    setPillPosition(clamped);

    if (fromScrub && target.ym !== lastHapticYm) {
      const now = performance.now();
      const yearChanged = lastHapticYear !== null && target.year !== lastHapticYear;
      if (yearChanged) {
        haptic('yearDetent');
        lastTickAt = now;
      } else if (now - lastTickAt >= TICK_THROTTLE_MS) {
        haptic('porcelainTick');
        lastTickAt = now;
      }
      lastHapticYm = target.ym;
      lastHapticYear = target.year;
    }

    if (target.ym !== activeYm) {
      activeYm = target.ym;
      jumpAc?.abort();
      jumpAc = new AbortController();
      onJumpMonth?.(target, { instant: fromScrub, signal: jumpAc.signal });
    }
  }

  function indexFromClientY(clientY) {
    if (!cachedRect || !months.length) return 0;
    const ratio = Math.max(0, Math.min(1, (clientY - cachedRect.top) / Math.max(1, cachedRect.height)));
    return Math.round(ratio * (months.length - 1));
  }

  spineEl.addEventListener('pointerdown', (e) => {
    if (!enabled || !months.length) return;
    e.preventDefault();
    dragging = true;
    cachedRect = spineEl.getBoundingClientRect();
    hapticPrepare('porcelainTick');
    spineEl.classList.add('is-awake', 'is-scrubbing');
    clearTimeout(idleTimer);
    try { spineEl.setPointerCapture(e.pointerId); } catch { /* ignore */ }
    triggerIndex(indexFromClientY(e.clientY), { fromScrub: true });
  }, { signal: ac.signal });

  spineEl.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    e.preventDefault();
    triggerIndex(indexFromClientY(e.clientY), { fromScrub: true });
  }, { signal: ac.signal });

  const endScrub = () => {
    if (!dragging) return;
    dragging = false;
    cachedRect = null;
    spineEl.classList.remove('is-scrubbing');
    showBriefly();
  };
  spineEl.addEventListener('pointerup', endScrub, { passive: true, signal: ac.signal });
  spineEl.addEventListener('pointercancel', endScrub, { passive: true, signal: ac.signal });

  function update({ months: nextMonths = [], totalSlips = 0, activeYm: nextYm = '' } = {}) {
    months = nextMonths;
    enabled = totalSlips >= 25 || months.length >= 3;
    spineEl.style.display = enabled ? 'flex' : 'none';
    if (!enabled) {
      latestBtn.style.display = 'none';
      spineEl.classList.remove('is-awake', 'is-scrubbing');
      return;
    }
    clear(trackEl);
    const ticks = Math.min(months.length, 24);
    for (let i = 0; i < ticks; i++) {
      const m = months[Math.round((i / Math.max(1, ticks - 1)) * (months.length - 1))];
      trackEl.appendChild(h('span', { class: ['ghost-spine-tick', (m?.month === 1 || i === 0) && 'is-year'] }));
    }
    if (nextYm) {
      activeYm = nextYm;
      const idx = months.findIndex((m) => m.ym === nextYm);
      if (idx >= 0) {
        cachedRect = spineEl.getBoundingClientRect();
        setPillPosition(idx);
      }
    }
  }

  let lastShowLatest = false;

  function notifyScroll(scrollTop = 0, currentYm = '') {
    if (!enabled) return;
    showBriefly();
    const showLatest = scrollTop > 640;
    if (showLatest !== lastShowLatest) {
      lastShowLatest = showLatest;
      latestBtn.style.display = showLatest ? 'inline-flex' : 'none';
      latestBtn.classList.toggle('is-visible', showLatest);
    }
    if (currentYm && !dragging && currentYm !== activeYm) {
      activeYm = currentYm;
      const idx = months.findIndex((m) => m.ym === currentYm);
      if (idx >= 0) {
        cachedRect = cachedRect || spineEl.getBoundingClientRect();
        setPillPosition(idx);
      }
    }
  }

  function destroy() {
    clearTimeout(idleTimer);
    jumpAc?.abort();
    ac.abort();
    clear(spineEl);
    spineEl.style.display = 'none';
    latestBtn.remove();
  }

  return { update, notifyScroll, destroy };
}
