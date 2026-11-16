// fake-dom.mjs — the smallest DOM needed to unit-test dom.js without jsdom.
// Any attempt to set innerHTML marks the element so tests can assert it never happens with data.

class FakeStyle {
  constructor() {
    this.props = {};
  }
  setProperty(k, v) {
    this.props[k] = v;
  }
  getPropertyValue(k) {
    return this.props[k] || '';
  }
}

class FakeNode {
  constructor() {
    this.childNodes = [];
    this.parentNode = null;
  }
  appendChild(n) {
    if (n.nodeType === 11) {
      for (const c of [...n.childNodes]) this.appendChild(c);
      n.childNodes = [];
      return n;
    }
    n.parentNode = this;
    this.childNodes.push(n);
    return n;
  }
  removeChild(n) {
    this.childNodes = this.childNodes.filter((c) => c !== n);
    n.parentNode = null;
    return n;
  }
  get firstChild() {
    return this.childNodes[0] || null;
  }
  get textContent() {
    return this.childNodes.map((c) => c.textContent).join('');
  }
  set textContent(v) {
    this.childNodes = [];
    if (v !== '') this.appendChild(new FakeText(String(v)));
  }
}

class FakeText extends FakeNode {
  constructor(data) {
    super();
    this.nodeType = 3;
    this.data = data;
  }
  get textContent() {
    return this.data;
  }
  serialize() {
    return this.data.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
}

class FakeFragment extends FakeNode {
  constructor() {
    super();
    this.nodeType = 11;
  }
}

class FakeElement extends FakeNode {
  constructor(tag) {
    super();
    this.nodeType = 1;
    this.tagName = tag.toUpperCase();
    this.attributes = {};
    this.style = new FakeStyle();
    this.dataset = {};
    this.listeners = {};
    this.className = '';
    this.rawHtml = null;
    const self = this;
    this.classList = {
      add: (c) => (self.className = [...new Set(self.className.split(' ').filter(Boolean).concat(c))].join(' ')),
      remove: (c) => (self.className = self.className.split(' ').filter((x) => x && x !== c).join(' ')),
      contains: (c) => self.className.split(' ').includes(c),
      toggle: (c, on) => (on ?? !self.classList.contains(c) ? self.classList.add(c) : self.classList.remove(c)),
    };
  }
  setAttribute(k, v) {
    this.attributes[k] = String(v);
  }
  getAttribute(k) {
    return this.attributes[k] ?? null;
  }
  addEventListener(type, fn) {
    (this.listeners[type] ||= []).push(fn);
  }
  set innerHTML(v) {
    this.rawHtml = String(v);
    this.childNodes = [];
  }
  get innerHTML() {
    return this.rawHtml ?? this.childNodes.map((c) => c.serialize()).join('');
  }
  get children() {
    return this.childNodes.filter((c) => c.nodeType === 1);
  }
  serialize() {
    const attrs = Object.entries(this.attributes)
      .map(([k, v]) => ` ${k}="${v.replace(/"/g, '&quot;')}"`)
      .join('');
    const cls = this.className ? ` class="${this.className}"` : '';
    return `<${this.tagName.toLowerCase()}${cls}${attrs}>${this.innerHTML}</${this.tagName.toLowerCase()}>`;
  }
  get outerHTML() {
    return this.serialize();
  }
  /** count descendant elements with a given tag */
  countTag(tag) {
    let n = 0;
    for (const c of this.childNodes) {
      if (c.nodeType === 1) {
        if (c.tagName === tag.toUpperCase()) n++;
        n += c.countTag(tag);
      }
    }
    return n;
  }
}

export function installFakeDom() {
  const document = {
    createElement: (t) => new FakeElement(t),
    createTextNode: (d) => new FakeText(d),
    createDocumentFragment: () => new FakeFragment(),
  };
  globalThis.document = document;
  return document;
}
