// lib/journal-pinch.js — 5-State Pinch-vs-Fling FSM with Passive touchstart + Dynamic window touchmove (<= 190L)

import { animateSpring } from './spring-waapi.js';

const MIN_SPAN_PX = 28;
const ARM_DELTA_PX = 18;
const ZOOM_OUT_RATIO = 0.78;
const ZOOM_IN_RATIO = 1.24;
const COOLDOWN_MS = 150;
const MIN_SCALE_CLAMP = 0.84;
const MAX_SCALE_CLAMP = 1.18;

function touchSpan(t0, t1) {
  return Math.hypot(t1.clientX - t0.clientX, t1.clientY - t0.clientY);
}

function touchAngle(t0, t1) {
  return Math.atan2(t1.clientY - t0.clientY, t1.clientX - t0.clientX);
}

function isOverscrolling(scroller) {
  if (!scroller) return false;
  const maxTop = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
  return scroller.scrollTop < -1 || scroller.scrollTop > maxTop + 1;
}

/**
 * Attaches a 5-state (IDLE / VERTICAL_FLING / ARM_PINCH / PINCH_ACTIVE / COOLDOWN)
 * two-finger pinch recognizer to the journal scroller.
 * Keeps 1-finger vertical scroll 100% passive; attaches non-passive window touchmove
 * strictly while touches.length === 2 && span >= 28px.
 */
export function attachJournalPinch({ scroller, virtualRiver, drawerArbiter, stageEl, onZoomOut, onZoomIn, signal } = {}) {
  if (!scroller) return { destroy() {} };

  let state = 'IDLE'; // IDLE | VERTICAL_FLING | ARM_PINCH | PINCH_ACTIVE | COOLDOWN
  let startSpan = 0;
  let startAngle = 0;
  let lastRatio = 1;
  let lastScrollAt = 0;
  let cooldownTimer = 0;
  let moveBound = false;

  const setHold = (active) => {
    if (virtualRiver && typeof virtualRiver.setPinchHold === 'function') {
      virtualRiver.setPinchHold(active);
    }
  };

  const resetStageVisual = (animated) => {
    if (!stageEl) return;
    if (animated && lastRatio !== 1) {
      const fromScale = Math.max(MIN_SCALE_CLAMP, Math.min(MAX_SCALE_CLAMP, lastRatio));
      stageEl.style.transform = '';
      animateSpring(stageEl, [
        { transform: `scale3d(${fromScale.toFixed(3)}, ${fromScale.toFixed(3)}, 1)` },
        { transform: 'scale3d(1, 1, 1)' },
      ], { stiffness: 380, damping: 34, durationMs: 240 });
    } else {
      stageEl.style.transform = '';
    }
  };

  const unbindWindowMove = () => {
    if (!moveBound) return;
    moveBound = false;
    window.removeEventListener('touchmove', onWindowTouchMove, { passive: false });
  };

  const enterCooldown = () => {
    unbindWindowMove();
    setHold(false);
    state = 'COOLDOWN';
    clearTimeout(cooldownTimer);
    cooldownTimer = setTimeout(() => {
      if (state === 'COOLDOWN') state = 'IDLE';
    }, COOLDOWN_MS);
  };

  const onWindowTouchMove = (e) => {
    if (e.touches.length !== 2) {
      resetStageVisual(true);
      enterCooldown();
      return;
    }
    const span = touchSpan(e.touches[0], e.touches[1]);
    const angle = touchAngle(e.touches[0], e.touches[1]);
    const dSpan = Math.abs(span - startSpan);
    const dAngle = Math.abs(angle - startAngle);

    if (state === 'ARM_PINCH') {
      // Ignore pure two-finger rotation twists before scale crosses arm threshold
      if (dAngle > 0.32 && dSpan < ARM_DELTA_PX) {
        enterCooldown();
        return;
      }
      if (dSpan < ARM_DELTA_PX) return;
      state = 'PINCH_ACTIVE';
      setHold(true);
      if (drawerArbiter && typeof drawerArbiter.abort === 'function') drawerArbiter.abort();
    }

    if (state === 'PINCH_ACTIVE') {
      if (e.cancelable) e.preventDefault();
      lastRatio = span / Math.max(1, startSpan);
      if (stageEl) {
        const visual = Math.max(MIN_SCALE_CLAMP, Math.min(MAX_SCALE_CLAMP, lastRatio));
        stageEl.style.transform = `scale3d(${visual.toFixed(3)}, ${visual.toFixed(3)}, 1)`;
      }
    }
  };

  const onTouchStart = (e) => {
    if (state === 'COOLDOWN') return;
    if (e.touches.length === 1) {
      if (performance.now() - lastScrollAt < 90) state = 'VERTICAL_FLING';
      return;
    }
    if (e.touches.length !== 2) {
      if (state === 'PINCH_ACTIVE') {
        resetStageVisual(true);
        enterCooldown();
      }
      return;
    }
    if (isOverscrolling(scroller)) return;

    const span = touchSpan(e.touches[0], e.touches[1]);
    if (span < MIN_SPAN_PX) return;

    startSpan = span;
    startAngle = touchAngle(e.touches[0], e.touches[1]);
    lastRatio = 1;
    state = 'ARM_PINCH';

    if (!moveBound) {
      moveBound = true;
      window.addEventListener('touchmove', onWindowTouchMove, { passive: false });
    }
  };

  const onTouchFinish = (e) => {
    if (state === 'PINCH_ACTIVE') {
      const ratio = lastRatio;
      resetStageVisual(true);
      enterCooldown();
      if (ratio <= ZOOM_OUT_RATIO && typeof onZoomOut === 'function') {
        onZoomOut({ ratio });
      } else if (ratio >= ZOOM_IN_RATIO && typeof onZoomIn === 'function') {
        onZoomIn({ ratio });
      }
      return;
    }
    if (e.touches.length < 2) {
      unbindWindowMove();
      if (state === 'ARM_PINCH' || state === 'VERTICAL_FLING') state = 'IDLE';
    }
  };

  const onScroll = () => {
    lastScrollAt = performance.now();
  };

  const destroy = () => {
    clearTimeout(cooldownTimer);
    unbindWindowMove();
    setHold(false);
    resetStageVisual(false);
    scroller.removeEventListener('touchstart', onTouchStart);
    scroller.removeEventListener('touchend', onTouchFinish);
    scroller.removeEventListener('touchcancel', onTouchFinish);
    scroller.removeEventListener('scroll', onScroll);
  };

  scroller.addEventListener('touchstart', onTouchStart, { passive: true });
  scroller.addEventListener('touchend', onTouchFinish, { passive: true });
  scroller.addEventListener('touchcancel', onTouchFinish, { passive: true });
  scroller.addEventListener('scroll', onScroll, { passive: true });

  if (signal) {
    if (signal.aborted) destroy();
    else signal.addEventListener('abort', destroy, { once: true });
  }

  return { destroy };
}
