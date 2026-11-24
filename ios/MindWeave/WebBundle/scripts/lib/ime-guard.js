// lib/ime-guard.js — 4-layer Chinese Pinyin IME guard (Safari WebKit compositionend + keyCode 229 safe, <= 80L)

const POST_COMPOSITION_GRACE_MS = 120;

function detectTouchDevice() {
  if (typeof window === 'undefined') return false;
  if (window.matchMedia && window.matchMedia('(pointer: coarse)').matches) return true;
  return 'ontouchstart' in window || (typeof navigator !== 'undefined' && navigator.maxTouchPoints > 0);
}

/**
 * Guards Enter-to-send against Safari/WKWebView Chinese Pinyin candidate confirmation.
 * 4 layers: explicit compositionstart/end flag, event.isComposing, event.keyCode === 229,
 * and a one-shot 120ms post-compositionend cooldown. Touch devices keep Return as newline.
 */
export function createImeGuard(textarea, { onSubmit, isTouchDevice } = {}) {
  let composing = false;
  let lastCompEndAt = -Infinity;
  const touch = typeof isTouchDevice === 'boolean' ? isTouchDevice : detectTouchDevice();

  const onCompStart = () => {
    composing = true;
  };

  const onCompEnd = () => {
    composing = false;
    lastCompEndAt = performance.now();
  };

  const isBlocked = (e) => {
    if (composing) return true;
    if (e && (e.isComposing || e.keyCode === 229)) return true;
    const dt = performance.now() - lastCompEndAt;
    if (dt >= 0 && dt < POST_COMPOSITION_GRACE_MS) {
      // One-shot consume so a genuine Enter right after grace isn't stuck
      lastCompEndAt = -Infinity;
      return true;
    }
    return false;
  };

  const shouldSendOnEnter = (e) => {
    if (!e || e.key !== 'Enter') return false;
    if (e.shiftKey || e.altKey || e.ctrlKey || e.metaKey) return false;
    if (touch) return false;
    return !isBlocked(e);
  };

  const onKeyDown = (e) => {
    if (e.key !== 'Enter') return;
    if (shouldSendOnEnter(e)) {
      e.preventDefault();
      if (typeof onSubmit === 'function') onSubmit(e);
    }
  };

  if (textarea && typeof textarea.addEventListener === 'function') {
    textarea.addEventListener('compositionstart', onCompStart);
    textarea.addEventListener('compositionend', onCompEnd);
    textarea.addEventListener('keydown', onKeyDown);
  }

  return {
    isBlocked,
    isComposing: () => composing,
    shouldSendOnEnter,
    destroy() {
      if (!textarea || typeof textarea.removeEventListener !== 'function') return;
      textarea.removeEventListener('compositionstart', onCompStart);
      textarea.removeEventListener('compositionend', onCompEnd);
      textarea.removeEventListener('keydown', onKeyDown);
    },
  };
}
