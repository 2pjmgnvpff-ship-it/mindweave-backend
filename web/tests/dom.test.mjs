import { test } from 'node:test';
import assert from 'node:assert/strict';
import { installFakeDom } from './helpers/fake-dom.mjs';

installFakeDom();
const { h, escapeHtml, markedText, plainFromMarked, svg, replaceChildren } = await import('../scripts/dom.js');

const EVIL = '<img src=x onerror=alert(1)>';

test('a person named like an XSS payload renders as plain text', () => {
  const el = h('strong', { class: 'dossier-person-name', text: EVIL });
  assert.equal(el.textContent, EVIL);
  assert.equal(el.countTag('img'), 0);
  assert.equal(el.rawHtml, null, 'innerHTML must never be used');
  assert.match(el.outerHTML, /&lt;img src=x onerror=alert\(1\)&gt;/);
});

test('children strings become text nodes, not markup', () => {
  const el = h('div', null, '前缀 ', EVIL, h('span', { text: EVIL }));
  assert.equal(el.countTag('img'), 0);
  assert.equal(el.textContent, `前缀 ${EVIL}${EVIL}`);
});

test('attributes are set via setAttribute (values not parsed as HTML)', () => {
  const el = h('button', { 'aria-label': EVIL, title: '"><script>' });
  assert.equal(el.getAttribute('aria-label'), EVIL);
  assert.equal(el.countTag('script'), 0);
});

test('h() refuses innerHTML props', () => {
  assert.throws(() => h('div', { innerHTML: EVIL }), /unsafe/);
});

test('escapeHtml escapes all five specials', () => {
  assert.equal(escapeHtml(`<a href="x">'&'</a>`), '&lt;a href=&quot;x&quot;&gt;&#39;&amp;&#39;&lt;/a&gt;');
});

test('markedText keeps only <mark> and never parses other tags', () => {
  const host = h('p');
  replaceChildren(host, markedText(`跟<mark>妈妈</mark>说 &lt;b&gt; ${EVIL}`));
  assert.equal(host.countTag('mark'), 1);
  assert.equal(host.countTag('img'), 0);
  assert.equal(host.countTag('b'), 0);
  assert.match(host.textContent, /^跟妈妈说 <b> $/);
  assert.equal(plainFromMarked('跟<mark>妈妈</mark>说好5月12日回家'), '跟妈妈说好5月12日回家');
});

test('svg() is the single innerHTML use and only for constant markup', () => {
  const s = svg('<svg viewBox="0 0 1 1"></svg>', 'icon');
  assert.equal(s.rawHtml, '<svg viewBox="0 0 1 1"></svg>');
  assert.equal(s.getAttribute('aria-hidden'), 'true');
});
