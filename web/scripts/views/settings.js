// settings.js — the personal settings sheet (gear, top-left): paper texture, export, sign out.
// Paper textures live only here (the old director panel is gone). Default 暖棉水彩.
import { h } from '../dom.js';
import { api } from '../api/endpoints.js';
import { store, EDITIONS } from '../store.js';
import { openSheet, closeSheet } from './sheet.js';
import { whisper } from './toast.js';

export const EDITION_LABEL = {
  cotton: '暖棉水彩',
  dotgrid: '点阵胶片',
  parchment: '羊皮纸相册',
  traveler: '旅行者手帐',
  hobonichi: '一日一页',
  museglass: '缪斯玻璃',
};

const LOCAL_KEY = 'mw.edition';

export function applyEdition(edition, { persist = false } = {}) {
  const e = EDITIONS.includes(edition) ? edition : 'cotton';
  store.set({ edition: e });
  try {
    localStorage.setItem(LOCAL_KEY, e);
  } catch {
    /* private mode */
  }
  if (persist) {
    api
      .patchPreferences({ preferredEdition: e })
      .then((prefs) => store.set({ preferences: { ...(store.get().preferences || {}), ...prefs } }))
      .catch(() => whisper('换好了，只是暂时没同步到别的设备', 2600));
  }
}

export function initialEdition(prefs) {
  let local = null;
  try {
    local = localStorage.getItem(LOCAL_KEY);
  } catch {
    /* ignore */
  }
  return prefs?.preferredEdition || prefs?.edition || local || 'cotton';
}

export function openSettings({ onSignOut } = {}) {
  const current = store.get().edition;
  const cards = EDITIONS.map((e) =>
    h(
      'button',
      {
        type: 'button',
        class: ['edition-chip-card', e === current && 'is-active'],
        role: 'radio',
        'aria-checked': String(e === current),
        onClick: (ev) => {
          applyEdition(e, { persist: true });
          for (const c of ev.currentTarget.parentElement.children) {
            const on = c === ev.currentTarget;
            c.classList.toggle('is-active', on);
            c.setAttribute('aria-checked', String(on));
          }
        },
      },
      h('span', { class: ['edition-swatch', e], 'aria-hidden': 'true' }),
      h('span', { class: 'edition-name', text: EDITION_LABEL[e] }),
    ),
  );
  openSheet({
    title: '我的设置',
    body: [
      h('div', { class: 'sheet-section' }, h('p', { class: 'sheet-section-title', text: '纸张' }), h('div', { class: 'edition-grid', role: 'radiogroup', 'aria-label': '纸张' }, ...cards)),
      h(
        'div',
        { class: 'sheet-section' },
        h('p', { class: 'sheet-section-title', text: '我的手帐' }),
        h(
          'button',
          {
            type: 'button',
            class: 'sheet-row-btn',
            onClick: async () => {
              try {
                await api.requestExport();
                whisper('已开始打包，整理好后会通知你下载', 3200);
              } catch (err) {
                whisper(err.message, 3000);
              }
            },
          },
          h('span', { text: '导出全部手帐' }),
          h('small', { text: '属于你的，随时带走' }),
        ),
        h(
          'button',
          {
            type: 'button',
            class: 'sheet-row-btn is-danger',
            onClick: async () => {
              closeSheet();
              await api.logout();
              onSignOut?.();
            },
          },
          h('span', { text: '退出登录' }),
          h('small', { text: '手帐会安全地留在云端' }),
        ),
      ),
    ],
  });
}
