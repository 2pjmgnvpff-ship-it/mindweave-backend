// casual.js — 记繁琐 directory: zero-photo rolodex of crystal-tier people. Tap a row → their page.
import { h, clear, $ } from '../dom.js';
import { store } from '../store.js';
import { personDecor, monogramOf, sealStyle } from '../decor.js';
import { formatDateCn } from '../format.js';
import { actions } from '../actions.js';
import { haptic } from '../native-bridge.js';

let lastKey = '';

function row(p, justDemoted) {
  const d = personDecor(p);
  const latest = (p.materials || [])[0];
  const memo = p.shortMemo || latest?.snippet || latest?.body || '还没有记下什么';
  return h(
    'button',
    {
      type: 'button',
      role: 'listitem',
      class: ['casual-folio-row', justDemoted && 'is-just-demoted'],
      dataset: { personId: p.id },
      'aria-label': `${p.nameCn}${p.state === 'provisional' ? '，待确认' : ''}`,
      onClick: () => {
        haptic('light');
        actions.openPerson(p.id, { fromDirectory: true });
      },
    },
    h('span', { class: 'folio-side-tab', 'aria-hidden': 'true', style: { background: d.tabColor } }),
    h('span', { class: 'folio-monogram-seal', 'aria-hidden': 'true', style: sealStyle(p), text: monogramOf(p) }),
    h(
      'span',
      { class: 'folio-body' },
      h(
        'span',
        { class: 'folio-top-line' },
        h('span', { class: 'folio-name', text: p.nameCn }),
        p.roleTag ? h('span', { class: 'folio-badge', text: p.roleTag }) : null,
        p.state === 'provisional'
          ? h('span', { class: 'folio-provisional', text: '待确认' })
          : latest?.happenedOn
            ? h('span', { class: 'folio-date-whisper', text: formatDateCn(latest.happenedOn) })
            : null,
      ),
      h('span', { class: 'folio-memo', text: memo }),
    ),
    h('span', { class: 'folio-chevron', 'aria-hidden': 'true', text: '›' }),
  );
}

function render(s) {
  const hub = $('#casualHub');
  const people = s.casualIds.map((id) => s.people[id]).filter(Boolean);
  const key = [s.view, s.lastDemotedId, ...people.map((p) => `${p.id}:${p.version}:${p.state}`)].join('|');
  if (key === lastKey) return;
  lastKey = key;
  clear(hub);
  hub.setAttribute('aria-hidden', String(s.view !== 'directory'));
  hub.inert = s.view !== 'directory';
  hub.append(
    h(
      'header',
      { class: 'casual-hub-header' },
      h('div', {}, h('h2', { class: 'casual-hub-heading', text: '记繁琐' }), h('p', { class: 'casual-hub-sub', text: '日常往来的人，礼数周全不耗神' })),
      h('span', { class: 'casual-count-seal', text: `${people.length} 位` }),
    ),
  );
  const list = h('div', { class: 'casual-folio-directory', role: 'list' });
  if (!people.length) {
    list.appendChild(h('p', { class: 'casual-empty', text: '这里还空着。\n同事、邻居、老同学……说一句和 TA 有关的事，丘比特会把 TA 收进来。' }));
  }
  for (const p of people) list.appendChild(row(p, p.id === s.lastDemotedId));
  hub.appendChild(list);
}

export function openDirectory() {
  store.set({ view: 'directory' });
}

export function initCasual() {
  store.subscribe(render);
  render(store.get());
}
