// toast.js — single-slot opaque porcelain toast + 5.0s pause-on-touch Undo toast (0 blur).
import { h, clear, $ } from '../dom.js';
import { haptic } from '../native-bridge.js';

let current = null;

function ringSvg() {
  const ns = 'http://www.w3.org/2000/svg';
  const svgEl = document.createElementNS(ns, 'svg');
  svgEl.setAttribute('class', 'mw-toast-ring');
  svgEl.setAttribute('viewBox', '0 0 22 22');
  svgEl.setAttribute('aria-hidden', 'true');
  for (const cls of ['ring-track', 'ring-progress']) {
    const c = document.createElementNS(ns, 'circle');
    c.setAttribute('class', cls);
    c.setAttribute('cx', '11');
    c.setAttribute('cy', '11');
    c.setAttribute('r', '9');
    svgEl.appendChild(c);
  }
  return svgEl;
}

export function dismissToast() {
  if (!current) return;
  const entry = current;
  current = null;
  clearTimeout(entry.timer);
  clearTimeout(entry.removeTimer);
  entry.ac?.abort?.();
  entry.el.classList.remove('is-visible');
  entry.removeTimer = setTimeout(() => entry.el.remove(), 220);
}

/**
 * showToast('已收进「妈妈」的手帐', { actionLabel: '撤销', onAction, ms: 5000 })
 * Pauses countdown on touch / pointerenter / focus so user never loses an Undo tap!
 */
export function showToast(text, { actionLabel = '', onAction = null, ms = 5000, onExpire = null } = {}) {
  const host = $('#toastHost');
  if (!host) return { dismiss() {} };
  if (current) {
    clearTimeout(current.timer);
    clearTimeout(current.removeTimer);
    current.ac?.abort?.();
    current.el.remove();
    current = null;
  }
  clear(host);

  const ac = new AbortController();
  const el = h(
    'div',
    { class: ['mw-toast', !onAction && 'no-action'], style: { '--toast-ms': `${ms}ms` } },
    h('span', { class: 'mw-toast-text', text }),
  );

  let acted = false;
  let remaining = ms;
  let startedAt = Date.now();
  let paused = false;

  const expire = () => {
    if (current === handle) dismissToast();
    if (!acted && onExpire) onExpire();
  };

  const pauseTimer = () => {
    if (paused || acted) return;
    paused = true;
    remaining = Math.max(400, remaining - (Date.now() - startedAt));
    clearTimeout(handle.timer);
    el.classList.add('is-paused');
  };

  const resumeTimer = () => {
    if (!paused || acted || current !== handle) return;
    paused = false;
    startedAt = Date.now();
    el.classList.remove('is-paused');
    handle.timer = setTimeout(expire, remaining);
  };

  if (onAction) {
    const btn = h(
      'button',
      {
        type: 'button',
        class: 'mw-toast-undo',
        'aria-label': `${actionLabel || '撤销'}（${Math.round(ms / 1000)} 秒内有效）`,
        onClick: async () => {
          if (acted) return;
          acted = true;
          haptic('warning');
          btn.disabled = true;
          dismissToast();
          try { await onAction(); } catch { /* caller reports errors */ }
        },
      },
      ringSvg(),
      h('span', { text: actionLabel || '撤销' }),
    );
    el.appendChild(btn);
    el.addEventListener('pointerenter', pauseTimer, { signal: ac.signal });
    el.addEventListener('pointerleave', resumeTimer, { signal: ac.signal });
    el.addEventListener('touchstart', pauseTimer, { passive: true, signal: ac.signal });
    el.addEventListener('touchend', resumeTimer, { passive: true, signal: ac.signal });
    el.addEventListener('focusin', pauseTimer, { signal: ac.signal });
    el.addEventListener('focusout', resumeTimer, { signal: ac.signal });
  }

  host.appendChild(el);
  requestAnimationFrame(() => {
    if (current !== handle) return;
    el.classList.add('is-visible');
    if (onAction) el.classList.add('is-counting');
  });

  const handle = {
    el,
    ac,
    removeTimer: 0,
    timer: setTimeout(expire, ms),
    dismiss: () => current === handle && dismissToast(),
  };
  current = handle;
  return handle;
}

/** Explicit 5.0s Undo toast helper aligned with 03e & 05 §6. */
export function showUndoToast(text, onUndo, { actionLabel = '撤销', ms = 5000, onExpire = null } = {}) {
  return showToast(text, { actionLabel, onAction: onUndo, ms, onExpire });
}

/** Plain whisper (no action), shorter. */
export function whisper(text, ms = 2800) {
  return showToast(text, { ms });
}
