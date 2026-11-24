// lib/orb.js — Unified A1 5-Layer Living Pearlescent Orb (0 blur, 30fps RMS driver, battery freeze, <= 210L)

import { isMotionReduced } from './spring-waapi.js';

const VALID_SIZES = new Set(['dock', 'header', 'voice', 'starter']);
const VALID_STATES = new Set(['idle', 'listening', 'thinking', 'speaking', 'done']);
const RMS_MIN_INTERVAL_MS = 33; // ~30fps cap for thermal/battery discipline
const DONE_EXHALE_MS = 900;
const DONE_EXHALE_REDUCED_MS = 320;

const doneTimers = new WeakMap();
const lastRmsTick = new WeakMap();
const veilCache = new WeakMap();

function getVeil(orbEl) {
  if (!orbEl) return null;
  let veil = veilCache.get(orbEl);
  if (veil && orbEl.contains(veil)) return veil;
  veil = orbEl.querySelector('.mw-orb-veil');
  if (veil) veilCache.set(orbEl, veil);
  return veil;
}

function clearVeilInline(orbEl) {
  const veil = getVeil(orbEl);
  if (!veil) return;
  veil.style.transform = '';
  veil.style.opacity = '';
}

function clearDoneTimer(orbEl) {
  const id = doneTimers.get(orbEl);
  if (id) {
    clearTimeout(id);
    doneTimers.delete(orbEl);
  }
}

/**
 * Pure DOM factory for the 5-layer Living Pearlescent Orb (zero innerHTML).
 * Layers: porcelain ring (.mw-orb) -> vessel -> base + caustic + veil + specular.
 */
export function createOrb({ size = 'dock', state = 'idle' } = {}) {
  const safeSize = VALID_SIZES.has(size) ? size : 'dock';
  const safeState = VALID_STATES.has(state) ? state : 'idle';

  const root = document.createElement('span');
  root.className = 'mw-orb';
  root.dataset.orbSize = safeSize;
  root.dataset.orbState = safeState;
  root.setAttribute('aria-hidden', 'true');

  const vessel = document.createElement('span');
  vessel.className = 'mw-orb-vessel';

  const base = document.createElement('span');
  base.className = 'mw-orb-base';

  const caustic = document.createElement('span');
  caustic.className = 'mw-orb-caustic';

  const veil = document.createElement('span');
  veil.className = 'mw-orb-veil';

  const specular = document.createElement('span');
  specular.className = 'mw-orb-specular';

  vessel.append(base, caustic, veil, specular);
  root.appendChild(vessel);
  veilCache.set(root, veil);
  return root;
}

/**
 * Transitions the Living Orb between idle | listening | thinking | speaking | done.
 * Leaving 'listening' immediately clears inline RMS styles so CSS animations resume smoothly.
 * Entering 'done' performs the calm pale-still exhale and returns to 'idle' automatically.
 */
export function setOrbState(orbEl, state) {
  if (!orbEl) return;
  const next = VALID_STATES.has(state) ? state : 'idle';
  const prev = orbEl.dataset.orbState || 'idle';

  clearDoneTimer(orbEl);
  if (prev === 'listening' && next !== 'listening') {
    clearVeilInline(orbEl);
  }
  if (next !== 'listening') {
    lastRmsTick.delete(orbEl);
  }

  orbEl.dataset.orbState = next;

  if (next === 'done') {
    clearVeilInline(orbEl);
    const holdMs = isMotionReduced() ? DONE_EXHALE_REDUCED_MS : DONE_EXHALE_MS;
    const tid = setTimeout(() => {
      doneTimers.delete(orbEl);
      if (orbEl.dataset.orbState === 'done') {
        orbEl.dataset.orbState = 'idle';
      }
    }, holdMs);
    doneTimers.set(orbEl, tid);
  }
}

/**
 * 30fps-gated direct transform/opacity audio driver on .mw-orb-veil (zero CSS custom property churn).
 * Uses perceptual rms^0.65 curve so whispers register visibly while loud speech stays composed.
 */
export function driveOrbRms(orbEl, rmsRaw) {
  if (!orbEl) return;
  const now = performance.now();
  const prevTs = lastRmsTick.get(orbEl) || 0;
  if (now - prevTs < RMS_MIN_INTERVAL_MS) return;
  lastRmsTick.set(orbEl, now);

  if (isMotionReduced()) return;
  const veil = getVeil(orbEl);
  if (!veil) return;

  const clamped = Math.max(0, Math.min(1, Number(rmsRaw) || 0));
  const curved = Math.pow(clamped, 0.65);
  const scale = (0.86 + curved * 0.34).toFixed(3);
  const opacity = (0.28 + curved * 0.64).toFixed(3);
  veil.style.transform = `scale3d(${scale}, ${scale}, 1)`;
  veil.style.opacity = opacity;
}

/**
 * Freezes all orb animations whenever the document/WebView goes to background
 * to protect iPhone thermal & battery budgets.
 */
export function initOrbVisibilityGuard(rootEl) {
  const target = rootEl || (typeof document !== 'undefined' ? document.documentElement : null);
  if (!target || typeof document === 'undefined') return () => {};

  const sync = () => {
    target.classList.toggle('is-bg-paused', Boolean(document.hidden));
  };
  const onHide = () => target.classList.add('is-bg-paused');
  const onShow = () => target.classList.toggle('is-bg-paused', Boolean(document.hidden));

  sync();
  document.addEventListener('visibilitychange', sync);
  window.addEventListener('pagehide', onHide);
  window.addEventListener('pageshow', onShow);

  return () => {
    document.removeEventListener('visibilitychange', sync);
    window.removeEventListener('pagehide', onHide);
    window.removeEventListener('pageshow', onShow);
  };
}
