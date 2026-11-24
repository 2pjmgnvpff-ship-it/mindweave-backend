// sheet.js — Multi-level LIFO Frosted Sheet Stack (fixes P0-03 Peek z-index & P0-05 Archive Picker wipe!),
// 1:1 grabber swipe-down, dynamic title/sub update handle, and clean Safari 26 display:none teardown.
import { h, clear, $ } from '../dom.js';
import { haptic } from '../native-bridge.js';

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]):not([type="hidden"]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';
const CLOSE_MS = 220;

const sheetStack = [];

function topEntry() {
  return sheetStack.length ? sheetStack[sheetStack.length - 1] : null;
}

function syncOverlayState() {
  const layer = $('#sheetLayer');
  const peekOpen = Boolean($('#peekLayer')?.classList.contains('is-open'));
  if (layer) {
    layer.classList.toggle('is-above-peek', peekOpen && sheetStack.length > 0);
    layer.classList.toggle('is-open', sheetStack.length > 0);
    layer.classList.toggle('is-mounted', sheetStack.length > 0);
  }
  const hasAny = Boolean(sheetStack.length > 0 || peekOpen || $('#chat')?.classList.contains('is-active'));
  $('#app')?.classList.toggle('has-overlay', hasAny);
  for (const id of ['journalCanvas', 'dock']) {
    const el = $(`#${id}`);
    if (el) el.inert = hasAny;
  }
}

function attachGrabberDrag({ sheet, bodyEl, scrim, onDismiss, signal }) {
  let drag = null;

  const reset = () => {
    sheet.style.transition = '';
    sheet.style.transform = '';
    sheet.style.willChange = '';
    scrim.style.transition = '';
    scrim.style.opacity = '';
  };

  sheet.addEventListener('touchstart', (e) => {
    if (e.touches.length !== 1) return;
    const onHead = Boolean(e.target?.closest?.('.sheet-head, .sheet-grabber-hit, .sheet-grabber, .sheet-title, .sheet-sub, .vellum-head'));
    const atTop = !bodyEl || bodyEl.scrollTop <= 1;
    if (!onHead && !atTop) return;
    const t = e.touches[0];
    drag = { y0: t.clientY, x0: t.clientX, t0: performance.now(), dy: 0, locked: onHead };
  }, { passive: true, signal });

  sheet.addEventListener('touchmove', (e) => {
    if (!drag || e.touches.length !== 1) return;
    const t = e.touches[0];
    const dy = t.clientY - drag.y0;
    const dx = Math.abs(t.clientX - drag.x0);
    if (!drag.locked) {
      if (dy < 10 || dy < dx * 1.2) return;
      if (bodyEl && bodyEl.scrollTop > 1) { drag = null; return; }
      drag.locked = true;
    }
    if (dy <= 0) return;
    if (e.cancelable) e.preventDefault();
    drag.dy = dy;
    sheet.style.transition = 'none';
    sheet.style.willChange = 'transform';
    sheet.style.transform = `translate3d(0, ${dy}px, 0)`;
    const progress = Math.min(1, dy / 260);
    scrim.style.transition = 'none';
    scrim.style.opacity = String(Math.max(0.05, 1 - progress * 0.85));
  }, { passive: false, signal });

  const finish = () => {
    if (!drag) return;
    const { dy, t0, locked } = drag;
    drag = null;
    if (!locked || dy <= 0) { reset(); return; }
    const velocity = dy / Math.max(16, performance.now() - t0);
    if (dy > 100 || velocity > 0.55) {
      sheet.style.transition = `transform ${CLOSE_MS}ms cubic-bezier(0.22, 1, 0.36, 1)`;
      sheet.style.transform = 'translate3d(0, 100%, 0)';
      scrim.style.transition = `opacity ${CLOSE_MS}ms ease`;
      scrim.style.opacity = '0';
      onDismiss();
    } else {
      sheet.style.transition = 'transform 260ms cubic-bezier(0.22, 1, 0.36, 1)';
      sheet.style.transform = 'translate3d(0, 0, 0)';
      scrim.style.transition = 'opacity 220ms ease';
      scrim.style.opacity = '';
      setTimeout(reset, 270);
    }
  };

  sheet.addEventListener('touchend', finish, { passive: true, signal });
  sheet.addEventListener('touchcancel', finish, { passive: true, signal });
}

function closeEntry(entry, { silentHaptic = false } = {}) {
  const idx = sheetStack.indexOf(entry);
  if (idx < 0) return;
  sheetStack.splice(idx, 1);

  if (entry.rafId) cancelAnimationFrame(entry.rafId);
  if (entry.moRafId) cancelAnimationFrame(entry.moRafId);
  if (entry.focusTimerId) clearTimeout(entry.focusTimerId);
  if (entry.clearTimerId) clearTimeout(entry.clearTimerId);
  entry.ac.abort();
  entry.ro?.disconnect();
  entry.mo?.disconnect();
  if (!silentHaptic) haptic('light');
  entry.host.classList.remove('is-open');

  // Re-enable inert on the underlying sheet if one remains on the stack
  const underneath = topEntry();
  if (underneath) underneath.host.inert = false;
  syncOverlayState();

  entry.clearTimerId = setTimeout(() => {
    entry.host.remove();
    const layer = $('#sheetLayer');
    if (layer && sheetStack.length === 0 && !layer.children.length) {
      layer.style.display = 'none';
    }
  }, CLOSE_MS + 20);

  if (entry.onClose) entry.onClose();
  const r = entry.restore;
  if (r && typeof r.focus === 'function' && r.isConnected) r.focus({ preventScroll: true });
}

export function closeSheet({ silentHaptic = false, all = false } = {}) {
  if (!sheetStack.length) return;
  if (all) {
    while (sheetStack.length) closeEntry(sheetStack[sheetStack.length - 1], { silentHaptic: true });
    if (!silentHaptic) haptic('light');
    return;
  }
  closeEntry(topEntry(), { silentHaptic });
}

/**
 * openSheet({ title, sub, label, body, onClose, initialFocus, className, stacked })
 * Automatically stacks nested pickers (Date Picker, People Picker) on top of existing sheets or Peek
 * so user forms in「最近归档记录」or Peek Edit are NEVER destroyed!
 */
export function openSheet({ title, sub, label, body, onClose, initialFocus, className = '', stacked = false } = {}) {
  const layer = $('#sheetLayer');
  if (!layer) return { el: null, bodyEl: null, setHeader() {}, close() {} };

  if (!stacked && sheetStack.length) {
    closeSheet({ silentHaptic: true, all: true });
    clear(layer);
  }
  const prevTop = topEntry();
  if (prevTop) prevTop.host.inert = true;

  layer.style.display = 'flex';
  const depth = sheetStack.length;
  const ac = new AbortController();
  const titleId = 'sheetTitle' + Math.random().toString(36).slice(2, 7);
  const grabber = h('div', { class: 'sheet-grabber-hit', 'aria-hidden': 'true' }, h('div', { class: 'sheet-grabber' }));
  const titleEl = h('h2', { class: 'sheet-title', id: titleId, text: title || '', hidden: !title });
  const subEl = h('p', { class: 'sheet-sub', text: sub || '', hidden: !sub });
  const headEl = h('div', { class: ['sheet-head', !title && !sub && 'is-compact'] }, grabber, titleEl, subEl);
  const bodyNodes = Array.isArray(body) ? body : body ? [body] : [];
  const bodyEl = h('div', { class: 'sheet-body' }, ...bodyNodes);
  const sheet = h(
    'div',
    {
      class: ['mw-sheet', className],
      role: 'dialog',
      'aria-modal': 'true',
      tabindex: '-1',
      'aria-labelledby': title ? titleId : null,
      'aria-label': title ? null : label || '面板',
    },
    headEl,
    bodyEl,
  );
  let entry;
  const scrim = h('div', { class: 'mw-sheet-scrim', onClick: () => closeEntry(entry) });
  const host = h('div', { class: 'mw-sheet-stack-item', style: { 'z-index': String(2 + depth * 2) } }, scrim, sheet);
  layer.appendChild(host);

  entry = {
    layer,
    host,
    sheet,
    bodyEl,
    onClose,
    restore: document.activeElement,
    ac,
    ro: null,
    mo: null,
    rafId: 0,
    moRafId: 0,
    focusTimerId: 0,
    clearTimerId: 0,
  };

  const setHeader = ({ title: nextTitle, sub: nextSub } = {}) => {
    if (nextTitle !== undefined) {
      titleEl.textContent = nextTitle || '';
      titleEl.hidden = !nextTitle;
    }
    if (nextSub !== undefined) {
      subEl.textContent = nextSub || '';
      subEl.hidden = !nextSub;
    }
  };

  const syncBottomFade = () => {
    entry.moRafId = 0;
    const remaining = bodyEl.scrollHeight - bodyEl.scrollTop - bodyEl.clientHeight;
    bodyEl.classList.toggle('has-bottom-fade', remaining > 4);
  };
  bodyEl.addEventListener('scroll', syncBottomFade, { passive: true, signal: ac.signal });
  if (typeof ResizeObserver === 'function') {
    entry.ro = new ResizeObserver(syncBottomFade);
    entry.ro.observe(bodyEl);
  }
  if (typeof MutationObserver === 'function') {
    entry.mo = new MutationObserver(() => {
      if (!entry.moRafId) entry.moRafId = requestAnimationFrame(syncBottomFade);
    });
    entry.mo.observe(bodyEl, { childList: true, subtree: true });
  }

  attachGrabberDrag({ sheet, bodyEl, scrim, onDismiss: () => closeEntry(entry, { silentHaptic: true }), signal: ac.signal });

  document.addEventListener('keydown', (e) => {
    if (topEntry() !== entry) return;
    if (e.key === 'Escape') { e.preventDefault(); closeEntry(entry); return; }
    if (e.key !== 'Tab') return;
    const nodes = Array.from(sheet.querySelectorAll(FOCUSABLE)).filter((n) => n.offsetParent !== null);
    if (!nodes.length) { e.preventDefault(); sheet.focus({ preventScroll: true }); return; }
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (e.shiftKey && (document.activeElement === first || !sheet.contains(document.activeElement))) {
      e.preventDefault(); last.focus({ preventScroll: true });
    } else if (!e.shiftKey && (document.activeElement === last || !sheet.contains(document.activeElement))) {
      e.preventDefault(); first.focus({ preventScroll: true });
    }
  }, { signal: ac.signal });

  sheetStack.push(entry);
  syncOverlayState();
  entry.rafId = requestAnimationFrame(() => {
    entry.rafId = 0;
    if (!sheetStack.includes(entry)) return;
    host.classList.add('is-open');
    syncBottomFade();
    const target = initialFocus || sheet;
    if (target) {
      entry.focusTimerId = setTimeout(() => {
        entry.focusTimerId = 0;
        if (topEntry() === entry) { syncBottomFade(); target.focus({ preventScroll: true }); }
      }, 120);
    }
  });
  return { el: sheet, bodyEl, setHeader, close: (opts) => closeEntry(entry, opts) };
}

export function isSheetOpen() {
  return sheetStack.length > 0;
}

export function scrollSheetToTop(smooth = true) {
  const cur = topEntry();
  if (!cur?.bodyEl) return false;
  cur.bodyEl.scrollTo({ top: 0, behavior: smooth ? 'smooth' : 'auto' });
  return true;
}
