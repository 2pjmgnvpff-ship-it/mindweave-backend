// dom.js — tiny DOM helpers. All dynamic text goes through textContent / text nodes (<= 160L).
// Never assign HTML strings with data. The single exception is `svg()`, which only accepts
// constant SVG markup that ships with the app (see static-svg comment below).

const FORBIDDEN_PROPS = new Set(['innerHTML', 'outerHTML', 'insertAdjacentHTML']);
const loadedStyles = new Map();

export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  applyProps(el, props || {});
  append(el, children);
  return el;
}

export function applyProps(el, props) {
  for (const key of Object.keys(props)) {
    const value = props[key];
    if (value === null || value === undefined || value === false) continue;
    if (FORBIDDEN_PROPS.has(key)) throw new Error('unsafe property: ' + key);
    if (key === 'class' || key === 'className') {
      el.className = Array.isArray(value) ? value.filter(Boolean).join(' ') : String(value);
    } else if (key === 'text') {
      el.textContent = String(value);
    } else if (key === 'style' && typeof value === 'object') {
      for (const prop of Object.keys(value)) {
        const v = value[prop];
        if (v === null || v === undefined || v === '') continue;
        el.style.setProperty(prop, String(v));
      }
    } else if (key === 'dataset' && typeof value === 'object') {
      for (const dk of Object.keys(value)) {
        if (value[dk] !== null && value[dk] !== undefined) el.dataset[dk] = String(value[dk]);
      }
    } else if (key.startsWith('on') && typeof value === 'function') {
      el.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === 'value' || key === 'checked' || key === 'disabled' || key === 'hidden') {
      el[key] = value;
    } else if (value === true) {
      el.setAttribute(key, '');
    } else {
      el.setAttribute(key, String(value));
    }
  }
  return el;
}

export function append(el, children) {
  for (const child of flatten(children)) {
    if (child === null || child === undefined || child === false || child === true) continue;
    el.appendChild(typeof child === 'object' ? child : document.createTextNode(String(child)));
  }
  return el;
}

function flatten(list) {
  const out = [];
  for (const item of list) {
    if (Array.isArray(item)) out.push(...flatten(item));
    else out.push(item);
  }
  return out;
}

export function clear(el) {
  while (el && el.firstChild) el.removeChild(el.firstChild);
  return el;
}

export function replaceChildren(el, ...children) {
  clear(el);
  return append(el, children);
}

export function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

const ENTITY_MAP = { '&lt;': '<', '&gt;': '>', '&amp;': '&', '&quot;': '"', '&#39;': "'", '&#x27;': "'" };
export function decodeEntities(value) {
  return String(value ?? '').replace(/&(lt|gt|amp|quot|#39|#x27);/g, (m) => ENTITY_MAP[m] || m);
}

export function markedText(html) {
  const frag = document.createDocumentFragment();
  const source = String(html ?? '');
  const re = /<mark>([\s\S]*?)<\/mark>/g;
  let last = 0;
  let m;
  while ((m = re.exec(source))) {
    if (m.index > last) frag.appendChild(document.createTextNode(stripTags(source.slice(last, m.index))));
    const mark = document.createElement('mark');
    mark.textContent = stripTags(m[1]);
    frag.appendChild(mark);
    last = re.lastIndex;
  }
  if (last < source.length) frag.appendChild(document.createTextNode(stripTags(source.slice(last))));
  return frag;
}

export function plainFromMarked(html) {
  return stripTags(String(html ?? '').replace(/<\/?mark>/g, ''));
}

function stripTags(s) {
  return decodeEntities(String(s).replace(/<[^>]*>/g, ''));
}

export function svg(staticMarkup, className) {
  const span = document.createElement('span');
  if (className) span.className = className;
  span.setAttribute('aria-hidden', 'true');
  span.innerHTML = staticMarkup; // static-svg
  return span;
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

export function prefersReducedMotion() {
  if (typeof document !== 'undefined' && document.documentElement?.classList?.contains('mw-reduce-motion')) {
    return true;
  }
  return Boolean(globalThis.matchMedia && globalThis.matchMedia('(prefers-reduced-motion: reduce)').matches);
}

export function replayClass(el, cls, ms = 700) {
  if (!el) return;
  el.classList.remove(cls);
  void el.offsetWidth;
  el.classList.add(cls);
  setTimeout(() => el.classList.remove(cls), ms);
}

/** Idempotently async-loads non-critical CSS (chat.css, sheets.css) on demand. */
export function ensureStyle(href) {
  if (!href || typeof document === 'undefined') return Promise.resolve();
  if (loadedStyles.has(href)) return loadedStyles.get(href);
  const existing = document.querySelector(`link[rel="stylesheet"][href="${href}"]`);
  if (existing) {
    const ready = Promise.resolve();
    loadedStyles.set(href, ready);
    return ready;
  }
  const promise = new Promise((resolve) => {
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = href;
    link.onload = () => resolve();
    link.onerror = () => resolve();
    document.head.appendChild(link);
  });
  loadedStyles.set(href, promise);
  return promise;
}
