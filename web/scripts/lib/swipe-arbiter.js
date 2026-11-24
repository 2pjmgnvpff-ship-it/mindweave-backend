// lib/swipe-arbiter.js — Vaul/Embla 1:1 Horizontal Drawer Arbiter & Native Keyboard Viewport Bridge (<= 350L)

import { springLinearEasing, isMotionReduced } from './spring-waapi.js';
import { onNativeEvent, isNativeIos } from '../native-bridge.js';

const SLOP_PX = 10;
const AXIS_RATIO = 1.35;
const MAX_DRAG_DURATION_MS = 500;
const CLICK_GUARD_MS = 240;
const GHOST_FOCUS_MS = 200;
const RESIZE_GRACE_MS = 120;
const CLOSE_UNMOUNT_MS = 220;
const FALLBACK_KB_THRESHOLD_PX = 60;

function hasActiveTextSelection() {
  try {
    const sel = window.getSelection ? window.getSelection() : null;
    return Boolean(sel && sel.toString().trim().length > 0);
  } catch (_) {
    return false;
  }
}

function blurFocusedInput() {
  const el = document.activeElement;
  if (el && (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') && typeof el.blur === 'function') {
    el.blur();
  }
}

/**
 * Right-edge 1:1 Memory Drawer swipe arbiter (zero parent scale, display:none after 220ms close).
 * Honors all Vaul + Embla/Swiper touch guards: 10px slop, |dx|>1.35|dy|, startX>=24, cancelable,
 * touches>=2, window.innerWidth+120ms, text selection, 500ms, 240ms allowClick, 200ms ghost focus.
 */
export function attachHorizontalDrawer({
  surface,
  scrim,
  triggerArea,
  widthPx = 324,
  edgeGuardPx = 24,
  onOpen,
  onClose,
  signal,
} = {}) {
  if (!surface) {
    return { open() {}, close() {}, isOpen: () => false, abort() {}, destroy() {} };
  }

  let openState = false;
  let tracking = false;
  let dragging = false;
  let startX = 0;
  let startY = 0;
  let startTs = 0;
  let lastX = 0;
  let lastTs = 0;
  let cachedW = widthPx;
  let currentX = widthPx;
  let rafId = 0;
  let unmountTimer = 0;
  let lastResizeAt = 0;
  let suppressClickUntil = 0;
  let suppressFocusUntil = 0;
  let activeTouchCount = 0;

  const setMounted = (mounted) => {
    surface.classList.toggle('is-mounted', mounted);
    if (scrim) scrim.classList.toggle('is-mounted', mounted);
  };

  const applyFrame = (xPx) => {
    const clamped = Math.max(0, Math.min(cachedW, xPx));
    currentX = clamped;
    surface.style.transform = `translate3d(${clamped.toFixed(1)}px, 0, 0)`;
    if (scrim) {
      const ratio = 1 - clamped / Math.max(1, cachedW);
      scrim.style.opacity = ratio.toFixed(3);
    }
  };

  const scheduleFrame = (xPx) => {
    currentX = xPx;
    if (rafId) return;
    rafId = requestAnimationFrame(() => {
      rafId = 0;
      applyFrame(currentX);
    });
  };

  const releaseTo = (nextOpen, emitCallback = true) => {
    clearTimeout(unmountTimer);
    if (rafId) {
      cancelAnimationFrame(rafId);
      rafId = 0;
    }
    const wasOpen = openState;
    openState = nextOpen;

    const dur = isMotionReduced() ? 1 : (nextOpen ? 280 : CLOSE_UNMOUNT_MS);
    const easing = isMotionReduced() ? 'linear' : (nextOpen ? springLinearEasing({ stiffness: 380, damping: 36 }) : 'cubic-bezier(0.22, 1, 0.36, 1)');

    setMounted(true);
    surface.style.transition = `transform ${dur}ms ${easing}`;
    if (scrim) scrim.style.transition = `opacity ${dur}ms ease`;

    if (nextOpen) {
      blurFocusedInput();
      surface.classList.add('is-open');
      if (scrim) scrim.classList.add('is-open');
      applyFrame(0);
    } else {
      surface.classList.remove('is-open');
      if (scrim) scrim.classList.remove('is-open');
      applyFrame(cachedW);
      unmountTimer = setTimeout(() => {
        if (!openState) {
          setMounted(false);
          surface.style.transition = '';
          surface.style.transform = '';
          if (scrim) {
            scrim.style.transition = '';
            scrim.style.opacity = '';
          }
        }
      }, dur + 12);
    }

    if (emitCallback && nextOpen !== wasOpen) {
      if (nextOpen && typeof onOpen === 'function') onOpen();
      if (!nextOpen && typeof onClose === 'function') onClose();
    }
  };

  const abort = () => {
    if (!tracking && !dragging) return;
    tracking = false;
    dragging = false;
    releaseTo(openState, false);
  };

  const onStart = (x, y, target, touchesLen) => {
    if (performance.now() - lastResizeAt < RESIZE_GRACE_MS) return;
    if (touchesLen >= 2) { abort(); return; }
    if (hasActiveTextSelection()) return;

    const vw = window.innerWidth || 390;
    // Never fight iOS left-edge system back-swipe
    if (x < edgeGuardPx) return;
    // When closed, accept horizontal pulls from right half or trigger area
    if (!openState && target && target.closest && target.closest('input, textarea, select')) return;

    clearTimeout(unmountTimer);
    cachedW = Math.max(220, surface.offsetWidth || Math.min(widthPx, Math.round(vw * 0.86)));
    tracking = true;
    dragging = false;
    startX = x;
    startY = y;
    lastX = x;
    startTs = performance.now();
    lastTs = startTs;
  };

  const onMove = (x, y, e) => {
    if (!tracking) return;
    if (activeTouchCount >= 2) { abort(); return; }
    const dx = x - startX;
    const dy = y - startY;

    if (!dragging) {
      if (Math.hypot(dx, dy) < SLOP_PX) return;
      if (Math.abs(dx) <= Math.abs(dy) * AXIS_RATIO) {
        tracking = false;
        return;
      }
      // Closed drawer opens on right-to-left (dx < 0); open drawer closes on left-to-right (dx > 0)
      if (!openState && dx > 0) { tracking = false; return; }
      if (openState && dx < 0) { tracking = false; return; }
      dragging = true;
      setMounted(true);
      surface.style.transition = 'none';
      if (scrim) scrim.style.transition = 'none';
    }

    if (e && e.cancelable) e.preventDefault();
    lastX = x;
    lastTs = performance.now();
    const targetX = openState ? Math.max(0, dx) : Math.max(0, cachedW + dx);
    scheduleFrame(targetX);
  };

  const onEnd = () => {
    if (!tracking) return;
    const wasDragging = dragging;
    tracking = false;
    dragging = false;
    if (!wasDragging) return;

    const now = performance.now();
    suppressClickUntil = now + CLICK_GUARD_MS;
    suppressFocusUntil = now + GHOST_FOCUS_MS;

    const elapsed = Math.max(1, now - startTs);
    const dx = lastX - startX;
    const vx = dx / Math.max(16, now - lastTs + 16);
    const fastFlick = elapsed <= MAX_DRAG_DURATION_MS && Math.abs(vx) > 0.35;

    let shouldOpen = openState;
    if (fastFlick) {
      shouldOpen = dx < 0;
    } else {
      shouldOpen = currentX < cachedW * 0.55;
    }
    releaseTo(shouldOpen, true);
  };

  const onTouchStart = (e) => {
    activeTouchCount = e.touches.length;
    if (activeTouchCount >= 2) { abort(); return; }
    const t = e.touches[0];
    onStart(t.clientX, t.clientY, e.target, activeTouchCount);
  };
  const onTouchMove = (e) => {
    activeTouchCount = e.touches.length;
    if (!tracking) return;
    onMove(e.touches[0].clientX, e.touches[0].clientY, e);
  };
  const onTouchEnd = (e) => { activeTouchCount = e.touches.length; onEnd(); };
  const onScrimClick = () => { if (performance.now() >= suppressClickUntil && openState) releaseTo(false, true); };
  const onClickCapture = (e) => { if (performance.now() < suppressClickUntil) { e.preventDefault(); e.stopPropagation(); } };
  const onFocusCapture = (e) => { if (performance.now() < suppressFocusUntil && e.target?.blur) e.target.blur(); };
  const onResize = () => { lastResizeAt = performance.now(); abort(); };

  const host = triggerArea || surface.parentElement || surface;
  host.addEventListener('touchstart', onTouchStart, { passive: true });
  host.addEventListener('touchmove', onTouchMove, { passive: false });
  host.addEventListener('touchend', onTouchEnd, { passive: true });
  host.addEventListener('touchcancel', onTouchEnd, { passive: true });
  host.addEventListener('click', onClickCapture, true);
  host.addEventListener('focusin', onFocusCapture, true);
  window.addEventListener('resize', onResize, { passive: true });
  if (scrim) scrim.addEventListener('click', onScrimClick);

  const destroy = () => {
    clearTimeout(unmountTimer);
    if (rafId) cancelAnimationFrame(rafId);
    host.removeEventListener('touchstart', onTouchStart);
    host.removeEventListener('touchmove', onTouchMove);
    host.removeEventListener('touchend', onTouchEnd);
    host.removeEventListener('touchcancel', onTouchEnd);
    host.removeEventListener('click', onClickCapture, true);
    host.removeEventListener('focusin', onFocusCapture, true);
    window.removeEventListener('resize', onResize);
    if (scrim) scrim.removeEventListener('click', onScrimClick);
  };

  if (signal) {
    if (signal.aborted) destroy();
    else signal.addEventListener('abort', destroy, { once: true });
  }

  return {
    open() {
      cachedW = Math.max(220, surface.offsetWidth || widthPx);
      releaseTo(true, true);
    },
    close() {
      cachedW = Math.max(220, surface.offsetWidth || widthPx);
      releaseTo(false, true);
    },
    isOpen: () => openState,
    abort,
    destroy,
  };
}

/**
 * Syncs iOS native keyboard spring curve & inset (via Swift `keyboard.change`)
 * with a 60px Vaul visualViewport fallback for mobile browsers.
 */
export function initKeyboardViewportBridge({ onInsetChange, signal } = {}) {
  const root = document.documentElement;
  let currentInset = 0;
  let nativeDriven = false;

  const applyInset = (height, durationMs, curveCss) => {
    const px = Math.max(0, Math.round(Number(height) || 0));
    currentInset = px;
    if (durationMs != null && Number.isFinite(Number(durationMs))) {
      root.style.setProperty('--mw-kb-dur', `${Math.max(0, Math.round(Number(durationMs)))}ms`);
    }
    if (typeof curveCss === 'string' && curveCss.trim()) {
      root.style.setProperty('--mw-kb-curve', curveCss.trim());
    }
    root.style.setProperty('--mw-kb-inset', `${px}px`);
    root.classList.toggle('is-kb-open', px > 0);
    if (typeof onInsetChange === 'function') onInsetChange(px);
  };

  const offNative = onNativeEvent('keyboard.change', (ev) => {
    nativeDriven = true;
    applyInset(ev?.height ?? 0, ev?.durationMs ?? 250, ev?.curveCss);
  }, { signal });

  const vv = typeof window !== 'undefined' ? window.visualViewport : null;
  const onVvChange = () => {
    if (nativeDriven || isNativeIos()) return;
    if (!vv) return;
    const rawDelta = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
    const next = rawDelta >= FALLBACK_KB_THRESHOLD_PX ? rawDelta : 0;
    if (next !== currentInset) applyInset(next, 220);
  };

  if (vv) {
    vv.addEventListener('resize', onVvChange, { passive: true });
    vv.addEventListener('scroll', onVvChange, { passive: true });
  }

  const destroy = () => {
    offNative();
    if (vv) {
      vv.removeEventListener('resize', onVvChange);
      vv.removeEventListener('scroll', onVvChange);
    }
    applyInset(0, 0);
  };

  if (signal) {
    if (signal.aborted) destroy();
    else signal.addEventListener('abort', destroy, { once: true });
  }

  return {
    getInset: () => currentInset,
    destroy,
  };
}
