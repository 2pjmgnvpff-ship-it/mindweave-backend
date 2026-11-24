// crisis.js — #F7FAF8 sage-porcelain Crisis Sanctuary Card (01a / 03a / G11).
// Guarantees China NHC 12356 primary hotline works offline via bundled fallback and
// preserves direct `tel:` dialing on iOS (never routed through SFSafariViewController).
import { h } from '../dom.js';
import { haptic } from '../native-bridge.js';
import { openSheet, closeSheet } from './sheet.js';

export const OFFLINE_CRISIS_HOTLINES = Object.freeze([
  { name: '全国统一心理援助热线', phone: '12356', primary: true, note: '24 小时 · 卫健委官方免费热线' },
  { name: '希望 24 小时生命热线', phone: '400-161-9995', primary: false, note: '全天候中文倾听与支持' },
  { name: '北京心理危机干预热线', phone: '010-82951332', primary: false, note: '24 小时专业心理危机干预' },
  { name: '紧急医疗 / 报警救助', phone: '120', secondaryPhone: '110', primary: false, note: '如身处危险请立即拨打 120 或 110' },
]);

function cleanTel(phone) {
  return String(phone || '').replace(/[^\d+]/g, '');
}

function mergeHotlines(rawList) {
  const seen = new Set();
  const list = [];
  const push = (item, primary = false) => {
    const tel = cleanTel(item?.phone);
    if (!tel || seen.has(tel)) return;
    seen.add(tel);
    list.push({
      name: String(item?.name || '心理援助热线').trim(),
      phone: String(item?.phone || tel).trim(),
      tel,
      note: item?.note ? String(item.note).trim() : '',
      primary: Boolean(primary || item?.primary || tel === '12356'),
      secondaryPhone: item?.secondaryPhone ? cleanTel(item.secondaryPhone) : '',
    });
  };
  // Always ensure 12356 sits prominently as primary CTA even when backend payload omits it.
  const backend = Array.isArray(rawList) ? rawList : [];
  const backendPrimary = backend.find((l) => cleanTel(l?.phone) === '12356');
  push(backendPrimary || OFFLINE_CRISIS_HOTLINES[0], true);
  for (const l of backend) push(l, false);
  for (const fallback of OFFLINE_CRISIS_HOTLINES) push(fallback, false);
  return list;
}

/** Build the #F7FAF8 sage-porcelain Crisis Sanctuary Card (0 blur, direct tel: CTA). */
export function crisisCard(card = {}, { onContinue } = {}) {
  const hotlines = mergeHotlines(card?.hotlines);
  const primary = hotlines[0];
  const others = hotlines.slice(1);

  const primaryCta = primary
    ? h(
        'a',
        {
          class: 'crisis-primary-cta',
          href: `tel:${primary.tel}`,
          'aria-label': `立即拨打${primary.name} ${primary.phone}`,
          onClick: () => haptic('medium'),
        },
        h('span', { class: 'crisis-cta-number', text: primary.phone }),
        h(
          'span',
          { class: 'crisis-cta-copy' },
          h('strong', { text: primary.name }),
          primary.note ? h('small', { text: primary.note }) : null,
        ),
        h('span', { class: 'crisis-cta-action', text: '拨打' }),
      )
    : null;

  const secondaryRows = others.map((l) =>
    h(
      'li',
      { class: 'crisis-hotline-item' },
      h(
        'a',
        {
          class: 'crisis-hotline-link',
          href: `tel:${l.tel}`,
          'aria-label': `拨打${l.name} ${l.phone}`,
          onClick: () => haptic('light'),
        },
        h('span', { class: 'crisis-hotline-name', text: l.name }),
        h('span', { class: 'crisis-hotline-phone', text: l.phone }),
      ),
      l.secondaryPhone
        ? h(
            'a',
            {
              class: 'crisis-hotline-alt',
              href: `tel:${l.secondaryPhone}`,
              text: `或拨打 ${l.secondaryPhone}`,
              onClick: () => haptic('light'),
            },
          )
        : null,
    ),
  );

  return h(
    'section',
    { class: 'crisis-sanctuary-card crisis-card', role: 'alert', 'aria-live': 'assertive' },
    h('h3', { class: 'crisis-title', text: String(card?.title || '此刻你不必独自扛着') }),
    h(
      'p',
      {
        class: 'crisis-body',
        text: String(
          card?.careMessage ||
            '如果现在心里很沉重、很难熬，随时可以找专业的人说说话。无论几点，电话那头都有人听你说。',
        ),
      },
    ),
    primaryCta,
    secondaryRows.length ? h('ul', { class: 'crisis-hotlines' }, ...secondaryRows) : null,
    h(
      'p',
      {
        class: 'crisis-disclaimer',
        text: String(card?.disclaimer || '心络是个人记录工具，无法替代专业心理或医疗危机干预'),
      },
    ),
    onContinue
      ? h('button', {
          type: 'button',
          class: 'crisis-continue',
          text: '我知道了，先回到安静的页面',
          onClick: () => {
            haptic('light');
            onContinue();
          },
        })
      : null,
  );
}

/** Present Crisis Sanctuary Card inside a calm zero-blur porcelain sheet. */
export function openCrisisCard(card = {}, { onContinue } = {}) {
  haptic('gentleWarn');
  openSheet({
    label: '此刻陪伴与求助',
    body: crisisCard(card, {
      onContinue: () => {
        closeSheet();
        onContinue?.();
      },
    }),
  });
}
