// gestures.js — Apple iOS 18 HIG touch physics helpers:
// 1. Finger-tracked swipe-down-to-dismiss with touchmove scroll-lock (zero pointercancel),
//    post-drag click suppression, damped rubber-band resistance & velocity fling.
// 2. Continuous vertical finger scrub on right-edge paper index tabs with stable key tracking
//    and floating frosted Month Loupe.
import { h } from './dom.js';
import { haptic } from './native-bridge.js';

const IGNORE_SELECTOR = 'input, textarea, select, [contenteditable="true"]';

/**
 * Attach live finger-tracked downward swipe dismissal to a sheet or overlay card.
 * Returns a cleanup() function that detaches all listeners.
 */
export function attachSwipeDismiss({
  container,
  scrollEl = null,
  scrim,
  onDismiss,
  canStart,
  headerOnly = false,
  threshold = 96,
  velocityThreshold = 0.55,
  scaleOnDrag = false,
} = {}) {
  if (!container) return () => {};
  const scroller = scrollEl || container;
  let drag = null;
  let resetTimer = 0;
  let suppressClickUntil = 0;

  const clearInline = () => {
    container.style.transition = '';
    container.style.transform = '';
    container.style.opacity = '';
    if (scrim) {
      scrim.style.transition = '';
      scrim.style.opacity = '';
    }
  };

  const onDown = (e) => {
    if (e.button !== undefined && e.button !== 0) return;
    if (e.target?.closest?.(IGNORE_SELECTOR)) return;
    const fromHeader = Boolean(canStart?.(e));
    if (headerOnly && !fromHeader) return;
    if (!fromHeader && scroller.scrollTop > 1) return;
    clearTimeout(resetTimer);
    drag = {
      id: e.pointerId,
      x0: e.clientX,
      y0: e.clientY,
      lastY: e.clientY,
      lastT: performance.now(),
      t0: performance.now(),
      vy: 0,
      dy: 0,
      fromHeader,
      locked: false,
      aborted: false,
    };
  };

  const onTouchMove = (e) => {
    if (!drag || drag.aborted || !e.touches?.length) return;
    const t = e.touches[0];
    const dx = t.clientX - drag.x0;
    const dy = t.clientY - drag.y0;
    const pullingDownAtTop = dy > 0 && scroller.scrollTop <= 1 && Math.abs(dy) >= Math.abs(dx);
    if (e.cancelable && (drag.locked || drag.fromHeader || pullingDownAtTop)) {
      e.preventDefault();
    }
  };

  const onMove = (e) => {
    if (!drag || drag.id !== e.pointerId || drag.aborted) return;
    const dx = e.clientX - drag.x0;
    const dy = e.clientY - drag.y0;
    if (!drag.locked) {
      if (Math.hypot(dx, dy) < 7) return;
      if (Math.abs(dx) > Math.abs(dy) * 0.9 || (!drag.fromHeader && (dy <= 0 || scroller.scrollTop > 1))) {
        drag.aborted = true;
        return;
      }
      drag.locked = true;
      container.classList.add('is-dragging');
      container.style.transition = 'none';
      if (scrim) scrim.style.transition = 'none';
      try { container.setPointerCapture(e.pointerId); } catch { /* ignore */ }
    }
    if (e.cancelable) e.preventDefault();
    const now = performance.now();
    const dt = Math.max(1, now - drag.lastT);
    drag.vy = drag.vy * 0.45 + ((e.clientY - drag.lastY) / dt) * 0.55;
    drag.lastY = e.clientY;
    drag.lastT = now;
    drag.dy = dy;

    const shift = dy >= 0 ? dy : -Math.min(26, Math.pow(Math.abs(dy), 0.72) * 0.9);
    const scale = scaleOnDrag && dy > 0 ? Math.max(0.95, 1 - dy / 2600) : 1;
    container.style.transform = scale === 1
      ? `translate3d(0, ${shift.toFixed(1)}px, 0)`
      : `translate3d(0, ${shift.toFixed(1)}px, 0) scale(${scale.toFixed(4)})`;
    if (scrim && dy >= 0) scrim.style.opacity = String(Math.max(0.12, 1 - dy / 360));
  };

  const finish = (e, canceled) => {
    if (!drag || drag.id !== e.pointerId) return;
    const cur = drag;
    drag = null;
    if (!cur.locked) return;
    suppressClickUntil = performance.now() + 280;
    container.classList.remove('is-dragging');
    const avgVy = cur.dy / Math.max(1, performance.now() - cur.t0);
    const velocity = Math.max(cur.vy, avgVy);
    if (!canceled && (cur.dy > threshold || (cur.dy > 28 && velocity > velocityThreshold))) {
      haptic('light');
      container.style.transition = 'transform 0.24s cubic-bezier(0.32, 0.72, 0, 1), opacity 0.2s ease';
      container.style.transform = 'translate3d(0, 108%, 0)';
      if (scaleOnDrag) container.style.opacity = '0';
      if (scrim) {
        scrim.style.transition = 'opacity 0.2s ease';
        scrim.style.opacity = '0';
      }
      onDismiss?.({ fromSwipe: true });
      return;
    }
    container.style.transition = 'transform 0.36s cubic-bezier(0.22, 1.24, 0.36, 1)';
    container.style.transform = 'translate3d(0, 0, 0)';
    if (scrim) {
      scrim.style.transition = 'opacity 0.24s ease';
      scrim.style.opacity = '';
    }
    resetTimer = setTimeout(clearInline, 370);
  };

  const onUp = (e) => finish(e, false);
  const onCancel = (e) => finish(e, true);
  const onClickCapture = (e) => {
    if (performance.now() < suppressClickUntil) {
      e.preventDefault();
      e.stopPropagation();
    }
  };

  container.addEventListener('pointerdown', onDown);
  container.addEventListener('touchmove', onTouchMove, { passive: false });
  container.addEventListener('pointermove', onMove, { passive: false });
  container.addEventListener('pointerup', onUp);
  container.addEventListener('pointercancel', onCancel);
  container.addEventListener('click', onClickCapture, true);

  return () => {
    clearTimeout(resetTimer);
    container.removeEventListener('pointerdown', onDown);
    container.removeEventListener('touchmove', onTouchMove);
    container.removeEventListener('pointermove', onMove);
    container.removeEventListener('pointerup', onUp);
    container.removeEventListener('pointercancel', onCancel);
    container.removeEventListener('click', onClickCapture, true);
  };
}

/**
 * Attach continuous vertical finger scrub + floating frosted Month Loupe to #paperEdgeTabs.
 * Tracks stable tab keys (`tab.dataset.month || 'pin'`) so state re-renders never break a drag.
 */
export function attachEdgeTabScrub({ navEl, canvasEl, onSelectTab, getLabel }) {
  if (!navEl || !canvasEl) return () => {};
  const loupe = h('div', { class: 'edge-tab-loupe', 'aria-hidden': 'true' });
  canvasEl.appendChild(loupe);

  let scrub = null;
  let hideTimer = 0;

  const keyOf = (tab) => (tab ? tab.dataset.month || (tab.classList.contains('is-pin-tab') ? 'pin' : '') : '');

  const nearestTab = (clientY) => {
    const tabs = Array.from(navEl.querySelectorAll('.paper-edge-tab'));
    if (!tabs.length) return null;
    let best = tabs[0];
    let bestDist = Infinity;
    for (const tab of tabs) {
      const r = tab.getBoundingClientRect();
      const dist = Math.abs(clientY - (r.top + r.height / 2));
      if (dist < bestDist) {
        bestDist = dist;
        best = tab;
      }
    }
    return best;
  };

  const showLoupe = (tab) => {
    if (!tab) return;
    const text = getLabel?.(tab) || tab.textContent || '';
    if (!text) return;
    const cRect = canvasEl.getBoundingClientRect();
    const tRect = tab.getBoundingClientRect();
    const centerY = Math.max(88, Math.min(cRect.height - 110, tRect.top + tRect.height / 2 - cRect.top));
    loupe.textContent = text;
    loupe.style.top = `${Math.round(centerY)}px`;
    loupe.classList.add('is-visible');
  };

  const hideLoupe = () => {
    navEl.querySelectorAll('.is-scrubbing').forEach((n) => n.classList.remove('is-scrubbing'));
    loupe.classList.remove('is-visible');
  };

  const focusTab = (tab, instant) => {
    const k = keyOf(tab);
    if (!tab || !k || scrub?.lastKey === k) return;
    if (scrub) scrub.lastKey = k;
    navEl.querySelectorAll('.is-scrubbing').forEach((n) => n !== tab && n.classList.remove('is-scrubbing'));
    tab.classList.add('is-scrubbing');
    if (navEl.scrollHeight > navEl.clientHeight + 2) tab.scrollIntoView({ block: 'nearest' });
    haptic('selection');
    showLoupe(tab);
    onSelectTab?.(tab, { instant });
  };

  const onDown = (e) => {
    if (e.button !== undefined && e.button !== 0) return;
    clearTimeout(hideTimer);
    scrub = { id: e.pointerId, y0: e.clientY, moved: false, lastKey: '' };
  };

  const onMove = (e) => {
    if (!scrub || scrub.id !== e.pointerId) return;
    if (!scrub.moved) {
      if (Math.abs(e.clientY - scrub.y0) <= 4) return;
      scrub.moved = true;
      try { navEl.setPointerCapture(e.pointerId); } catch { /* ignore */ }
    }
    if (e.cancelable) e.preventDefault();
    if (navEl.scrollHeight > navEl.clientHeight + 2) {
      const nr = navEl.getBoundingClientRect();
      if (e.clientY < nr.top + 28) navEl.scrollTop -= 14;
      else if (e.clientY > nr.bottom - 28) navEl.scrollTop += 14;
    }
    focusTab(nearestTab(e.clientY), true);
  };

  const onEnd = (e) => {
    if (!scrub || scrub.id !== e.pointerId) return;
    const wasMoved = scrub.moved;
    scrub = null;
    if (wasMoved) {
      navEl.dataset.suppressClick = '1';
      setTimeout(() => delete navEl.dataset.suppressClick, 60);
    }
    hideTimer = setTimeout(hideLoupe, wasMoved ? 160 : 0);
  };

  navEl.addEventListener('pointerdown', onDown);
  navEl.addEventListener('pointermove', onMove, { passive: false });
  navEl.addEventListener('pointerup', onEnd);
  navEl.addEventListener('pointercancel', onEnd);

  return () => {
    clearTimeout(hideTimer);
    navEl.removeEventListener('pointerdown', onDown);
    navEl.removeEventListener('pointermove', onMove);
    navEl.removeEventListener('pointerup', onEnd);
    navEl.removeEventListener('pointercancel', onEnd);
    loupe.remove();
  };
}
