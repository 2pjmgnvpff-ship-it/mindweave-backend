// sheet.js — generic iOS 18 bottom sheet with 44pt grabber, finger-tracked swipe-down dismiss,
// background inert, focus trap, and top-toast overlay coordination.
import { h, clear, $ } from '../dom.js';
import { haptic } from '../native-bridge.js';
import { attachSwipeDismiss } from '../gestures.js';

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]):not([type="hidden"]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';
let open = null;

function syncOverlayState() {
  const hasAny = Boolean(
    open ||
    $('#peekLayer')?.classList.contains('is-open') ||
    $('#chat')?.classList.contains('is-active'),
  );
  $('#app')?.classList.toggle('has-overlay', hasAny);
  for (const id of ['journalCanvas', 'dock']) {
    const el = $(`#${id}`);
    if (el) el.inert = hasAny;
  }
}

export function closeSheet({ silentHaptic = false } = {}) {
  if (!open) return;
  const { layer, onClose, restore, keyHandler, cleanupDrag, cleanupFade, rafId } = open;
  open = null;
  if (rafId) cancelAnimationFrame(rafId);
  cleanupDrag?.();
  cleanupFade?.();
  if (!silentHaptic) haptic('light');
  layer.classList.remove('is-open');
  document.removeEventListener('keydown', keyHandler);
  syncOverlayState();
  setTimeout(() => {
    if (!open) clear(layer);
  }, 340);
  if (onClose) onClose();
  if (restore && typeof restore.focus === 'function' && restore.isConnected) restore.focus({ preventScroll: true });
}

/**
 * openSheet({ title, sub, label, body: Node|Node[], onClose, initialFocus })
 * Returns { el, close }.
 */
export function openSheet({ title, sub, label, body, onClose, initialFocus } = {}) {
  const layer = $('#sheetLayer');
  if (open) closeSheet({ silentHaptic: true });
  clear(layer);
  const titleId = 'sheetTitle' + Math.random().toString(36).slice(2, 7);
  const grabber = h('div', { class: 'sheet-grabber-hit', 'aria-hidden': 'true' }, h('div', { class: 'sheet-grabber' }));
  const headEl = h(
    'div',
    { class: ['sheet-head', !title && !sub && 'is-compact'] },
    grabber,
    title ? h('h2', { class: 'sheet-title', id: titleId, text: title }) : null,
    sub ? h('p', { class: 'sheet-sub', text: sub }) : null,
  );
  const bodyNodes = Array.isArray(body) ? body : body ? [body] : [];
  const bodyEl = h('div', { class: 'sheet-body' }, ...bodyNodes);
  const sheet = h(
    'div',
    { class: 'mw-sheet', role: 'dialog', 'aria-modal': 'true', tabindex: '-1', 'aria-labelledby': title ? titleId : null, 'aria-label': title ? null : label || '面板' },
    headEl,
    bodyEl,
  );
  const scrim = h('div', { class: 'mw-sheet-scrim', onClick: () => closeSheet() });
  layer.append(scrim, sheet);
  const syncBottomFade = () => {
    const remaining = bodyEl.scrollHeight - bodyEl.scrollTop - bodyEl.clientHeight;
    bodyEl.classList.toggle('has-bottom-fade', remaining > 4);
  };
  bodyEl.addEventListener('scroll', syncBottomFade, { passive: true });
  const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(syncBottomFade) : null;
  ro?.observe(bodyEl);
  const mo = typeof MutationObserver === 'function' ? new MutationObserver(() => requestAnimationFrame(syncBottomFade)) : null;
  mo?.observe(bodyEl, { childList: true, subtree: true });
  const cleanupFade = () => {
    bodyEl.removeEventListener('scroll', syncBottomFade);
    ro?.disconnect();
    mo?.disconnect();
  };
  const cleanupDrag = attachSwipeDismiss({
    container: sheet,
    scrollEl: bodyEl,
    scrim,
    canStart: (e) => Boolean(e.target?.closest?.('.sheet-head, .sheet-grabber-hit, .sheet-grabber, .sheet-title, .sheet-sub, .vellum-head')),
    onDismiss: () => closeSheet({ silentHaptic: true }),
  });
  const keyHandler = (e) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      closeSheet();
      return;
    }
    if (e.key !== 'Tab') return;
    const nodes = Array.from(sheet.querySelectorAll(FOCUSABLE)).filter((n) => n.offsetParent !== null);
    if (!nodes.length) {
      e.preventDefault();
      sheet.focus({ preventScroll: true });
      return;
    }
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (e.shiftKey && (document.activeElement === first || !sheet.contains(document.activeElement))) {
      e.preventDefault();
      last.focus({ preventScroll: true });
    } else if (!e.shiftKey && (document.activeElement === last || !sheet.contains(document.activeElement))) {
      e.preventDefault();
      first.focus({ preventScroll: true });
    }
  };
  document.addEventListener('keydown', keyHandler);
  const entry = { layer, onClose, restore: document.activeElement, keyHandler, cleanupDrag, cleanupFade, rafId: 0 };
  open = entry;
  syncOverlayState();
  entry.rafId = requestAnimationFrame(() => {
    if (open !== entry) return;
    layer.classList.add('is-open');
    syncBottomFade();
    const target = initialFocus || sheet.querySelector('textarea, input:not([type="hidden"]), button:not(.sheet-grabber)') || sheet;
    if (target) {
      setTimeout(() => {
        if (open === entry) {
          syncBottomFade();
          target.focus({ preventScroll: true });
        }
      }, 120);
    }
  });
  return { el: sheet, close: closeSheet };
}

export function isSheetOpen() {
  return Boolean(open);
}
