// toast.js — bottom whisper toast with a 5 s countdown ring and an optional 撤销 action.
// One toast at a time; host has role=status + aria-live=polite (see index.html).
import { h, clear, $ } from '../dom.js';

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
  const { el, timer } = current;
  clearTimeout(timer);
  el.classList.remove('is-visible');
  setTimeout(() => el.remove(), 260);
  current = null;
}

/**
 * showToast('已收进「妈妈」的手帐', { actionLabel: '撤销', onAction, ms: 5000 })
 * Returns a handle with dismiss().
 */
export function showToast(text, { actionLabel = '', onAction = null, ms = 5000, onExpire = null } = {}) {
  const host = $('#toastHost');
  if (!host) return { dismiss() {} };
  if (current) {
    clearTimeout(current.timer);
    current.el.remove();
    current = null;
  }
  clear(host);
  const el = h('div', { class: ['mw-toast', !onAction && 'no-action'], style: { '--toast-ms': `${ms}ms` } }, h('span', { class: 'mw-toast-text', text }));
  let acted = false;
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
          btn.disabled = true;
          dismissToast();
          try {
            await onAction();
          } catch {
            /* the caller shows its own message */
          }
        },
      },
      ringSvg(),
      h('span', { text: actionLabel || '撤销' }),
    );
    el.appendChild(btn);
  }
  host.appendChild(el);
  requestAnimationFrame(() => {
    el.classList.add('is-visible');
    if (onAction) el.classList.add('is-counting');
  });
  const handle = {
    el,
    timer: setTimeout(() => {
      if (current === handle) dismissToast();
      if (!acted && onExpire) onExpire();
    }, ms),
    dismiss: () => current === handle && dismissToast(),
  };
  current = handle;
  return handle;
}

/** Plain whisper (no action), shorter. */
export function whisper(text, ms = 2800) {
  return showToast(text, { ms });
}
