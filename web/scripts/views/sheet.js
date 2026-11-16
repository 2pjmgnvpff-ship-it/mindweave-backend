// sheet.js — one generic bottom sheet (settings, text capture, question notes).
import { h, clear, $ } from '../dom.js';

let open = null;

export function closeSheet() {
  if (!open) return;
  const { layer, onClose, restore, keyHandler, rafId } = open;
  open = null;
  if (rafId) cancelAnimationFrame(rafId);
  layer.classList.remove('is-open');
  document.removeEventListener('keydown', keyHandler);
  setTimeout(() => {
    if (!open) clear(layer);
  }, 340);
  if (onClose) onClose();
  if (restore && typeof restore.focus === 'function') restore.focus({ preventScroll: true });
}

/**
 * openSheet({ title, sub, label, body: Node|Node[], onClose, initialFocus })
 * Returns { el, close }.
 */
export function openSheet({ title, sub, label, body, onClose, initialFocus } = {}) {
  const layer = $('#sheetLayer');
  if (open) closeSheet();
  clear(layer);
  const titleId = 'sheetTitle' + Math.random().toString(36).slice(2, 7);
  const sheet = h(
    'div',
    { class: 'mw-sheet', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': title ? titleId : null, 'aria-label': title ? null : label || '面板' },
    h('div', { class: 'sheet-grabber', 'aria-hidden': 'true' }),
    title ? h('h2', { class: 'sheet-title', id: titleId, text: title }) : null,
    sub ? h('p', { class: 'sheet-sub', text: sub }) : null,
    body,
  );
  const scrim = h('div', { class: 'mw-sheet-scrim', onClick: () => closeSheet() });
  layer.append(scrim, sheet);
  const keyHandler = (e) => {
    if (e.key === 'Escape') closeSheet();
  };
  document.addEventListener('keydown', keyHandler);
  const entry = { layer, onClose, restore: document.activeElement, keyHandler, rafId: 0 };
  open = entry;
  entry.rafId = requestAnimationFrame(() => {
    if (open !== entry) return;
    layer.classList.add('is-open');
    const target = initialFocus || sheet.querySelector('textarea, input, button:not(.sheet-grabber)');
    if (target) {
      setTimeout(() => {
        if (open === entry) target.focus({ preventScroll: true });
      }, 120);
    }
  });
  return { el: sheet, close: closeSheet };
}

export function isSheetOpen() {
  return Boolean(open);
}
