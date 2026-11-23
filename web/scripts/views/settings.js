// settings.js — personal settings sheet (gear, top-left): paper texture, native server config, export, sign out.
// Paper textures live only here (default 暖棉水彩 cotton).
import { h } from '../dom.js';
import { api } from '../api/endpoints.js';
import { store, EDITIONS } from '../store.js';
import { openSheet, closeSheet } from './sheet.js';
import { whisper } from './toast.js';
import { haptic, isNativeIos, getConfiguredServerUrl, openServerConfig, sharePayload } from '../native-bridge.js';

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

function buildNativeServerSection() {
  if (!isNativeIos() && globalThis.location?.protocol !== 'mindweave:') return null;
  const currentUrl = getConfiguredServerUrl() || '默认（应用内代理）';
  return h(
    'div',
    { class: 'sheet-section' },
    h('p', { class: 'sheet-section-title', text: '连接' }),
    h(
      'button',
      {
        type: 'button',
        class: 'sheet-row-btn',
        onClick: () => {
          haptic('light');
          openServerConfig();
        },
      },
      h('span', { text: '连接服务器' }),
      h('small', { text: currentUrl }),
    ),
  );
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
          haptic('selection');
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
  const statusFooter = h('p', { class: 'settings-ai-whisper', hidden: true });
  api
    .aiStatus()
    .then((res) => {
      if (!statusFooter.isConnected || !res) return;
      const provider = res.aiPrimaryProvider || res.provider || '';
      const isLive =
        res.liveModelConfigured === true ||
        res.live === true ||
        res.connected === true ||
        res.aiLive === true ||
        res.mode === 'live' ||
        (typeof provider === 'string' && provider.length > 0 && !/fake|mock|stub|offline/i.test(provider) && res.liveModelConfigured !== false && res.live !== false);
      statusFooter.hidden = false;
      statusFooter.replaceChildren(
        h('span', { class: ['ai-whisper-dot', isLive && 'is-live'], 'aria-hidden': 'true' }),
        h('span', { text: isLive ? '丘比特灵感已在线' : '丘比特轻演示模式' }),
      );
    })
    .catch(() => {
      /* graceful no-op if unavailable */
    });

  openSheet({
    title: '我的设置',
    body: [
      h('div', { class: 'sheet-section' }, h('p', { class: 'sheet-section-title', text: '纸张' }), h('div', { class: 'edition-grid', role: 'radiogroup', 'aria-label': '纸张' }, ...cards)),
      buildNativeServerSection(),
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
              haptic('light');
              try {
                const res = await api.requestExport();
                haptic('success');
                const shareText = res?.downloadUrl || res?.summary || '';
                if (isNativeIos() && res?.downloadUrl) {
                  await sharePayload({ title: '心络手帐导出', url: res.downloadUrl, text: '心络手帐导出链接' });
                } else if (isNativeIos() && shareText) {
                  await sharePayload({ title: '心络手帐导出', text: shareText, filename: 'mindweave-export.txt' });
                } else if (isNativeIos()) {
                  await sharePayload({ title: '心络手帐导出', text: '心络手帐正在云端打包，完成后即可下载。' });
                }
                whisper('已开始打包，整理好后会通知你下载', 3200);
              } catch (err) {
                haptic('warning');
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
              haptic('warning');
              closeSheet({ silentHaptic: true });
              await api.logout();
              onSignOut?.();
            },
          },
          h('span', { text: '退出登录' }),
          h('small', { text: '手帐会安全地留在云端' }),
        ),
      ),
      statusFooter,
    ],
  });
}
