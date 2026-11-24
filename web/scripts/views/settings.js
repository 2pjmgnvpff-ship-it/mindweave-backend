// settings.js — A2 + C2 Serene 3-Group Porcelain Settings (纸面氛围 / 数据与隐私 / 账号).
// Zero Release dev server URLs (#if DEBUG only), zero demo status pill, real C2 Face ID lock
// w/ grace interval chips (0 / 60 / 300s), Step-Up .zip Export & Apple 5.1.1(v) Account Deletion.
import { h, clear } from '../dom.js';
import { api } from '../api/endpoints.js';
import { store, EDITIONS } from '../store.js';
import { openSheet, closeSheet } from './sheet.js';
import { whisper } from './toast.js';
import {
  haptic,
  isNativeIos,
  isDebugBuild,
  getConfiguredServerUrl,
  openServerConfig,
  mwBiometrics,
  mwNativeUI,
} from '../native-bridge.js';
import { actions } from '../actions.js';

export const EDITION_LABEL = Object.freeze({
  cotton: '晨光白瓷',
  hobonichi: '素雅细格',
  museglass: '柔光珠瓷',
});

const GRACE_OPTIONS = Object.freeze([
  { sec: 0, label: '切出即锁' },
  { sec: 60, label: '离开 1 分钟' },
  { sec: 300, label: '离开 5 分钟' },
]);

const LOCAL_KEY = 'mw.edition';

export function applyEdition(edition, { persist = false } = {}) {
  const valid = Array.isArray(EDITIONS) && EDITIONS.length ? EDITIONS : ['cotton', 'hobonichi', 'museglass'];
  const e = valid.includes(edition) ? edition : 'cotton';
  store.set({ edition: e });
  if (typeof document !== 'undefined') {
    const appEl = document.getElementById('app');
    if (appEl) appEl.dataset.journalEdition = e;
    if (document.documentElement) document.documentElement.dataset.journalEdition = e;
  }
  try {
    localStorage.setItem(LOCAL_KEY, e);
  } catch {
    /* private mode */
  }
  if (persist) {
    api
      .patchPreferences({ preferredEdition: e })
      .then((prefs) => store.set({ preferences: { ...(store.get().preferences || {}), ...prefs } }))
      .catch(() => whisper('已切换纸面氛围，稍后自动同步到其他设备', 2400));
  }
}

export function initialEdition(prefs) {
  let local = null;
  try {
    local = localStorage.getItem(LOCAL_KEY);
  } catch {
    /* ignore */
  }
  const raw = prefs?.preferredEdition || prefs?.edition || local || 'cotton';
  return Object.hasOwn(EDITION_LABEL, raw) ? raw : 'cotton';
}

function buildBiometricsBlock() {
  if (!isNativeIos() || typeof mwBiometrics?.getStatus !== 'function') return null;
  const host = h('div', { class: 'settings-biometric-block' });
  const labelEl = h('span', { text: '面容 ID 锁屏' });
  const subEl = h('small', { text: '离开心络后自动收好，再次打开时验证本人' });
  const toggleBtn = h('button', {
    type: 'button',
    role: 'switch',
    class: 'mw-switch-btn',
    'aria-checked': 'false',
    'aria-label': '面容 ID 锁屏',
  }, h('span', { class: 'mw-switch-thumb', 'aria-hidden': 'true' }));
  const graceRow = h('div', { class: 'settings-grace-row', role: 'radiogroup', 'aria-label': '自动锁定间隔', hidden: true });
  let state = { available: false, enabled: false, graceSeconds: 0, labelCn: '面容 ID', busy: false };

  function renderGrace() {
    clear(graceRow);
    graceRow.hidden = !state.enabled;
    for (const opt of GRACE_OPTIONS) {
      const active = Number(state.graceSeconds) === opt.sec;
      graceRow.appendChild(
        h('button', {
          type: 'button',
          role: 'radio',
          class: ['settings-grace-chip', active && 'is-active'],
          'aria-checked': String(active),
          text: opt.label,
          onClick: async () => {
            if (state.busy || active) return;
            haptic('selection');
            state.busy = true;
            try {
              const res = mwBiometrics.setGrace
                ? await mwBiometrics.setGrace(opt.sec)
                : await mwBiometrics.setEnabled?.({ enabled: true, graceSeconds: opt.sec });
              state.graceSeconds = res?.graceSeconds ?? opt.sec;
              renderGrace();
            } finally {
              state.busy = false;
            }
          },
        }),
      );
    }
  }

  function syncUi() {
    labelEl.textContent = `${state.labelCn || '面容 ID'} 锁屏`;
    toggleBtn.setAttribute('aria-checked', String(Boolean(state.enabled)));
    toggleBtn.classList.toggle('is-on', Boolean(state.enabled));
    toggleBtn.disabled = !state.available || state.busy;
    subEl.textContent = state.available
      ? '切回桌面时立即遮罩，保护手帐隐私'
      : '请先在 iPhone 系统设置中开启面容 ID 或密码';
    renderGrace();
  }

  toggleBtn.addEventListener('click', async () => {
    if (!state.available || state.busy) return;
    state.busy = true;
    syncUi();
    try {
      const target = !state.enabled;
      const res = await mwBiometrics.setEnabled(target);
      if (res?.ok) {
        state.enabled = Boolean(res.enabled);
        state.graceSeconds = res.graceSeconds ?? state.graceSeconds;
        haptic('memorySeal', { tier: 'gold' });
      } else {
        haptic('warning');
        if (res?.error) whisper(res.error, 2400);
      }
    } catch {
      haptic('warning');
      whisper('验证未完成，状态未变更', 2200);
    } finally {
      state.busy = false;
      syncUi();
    }
  });

  mwBiometrics
    .getStatus()
    .then((res) => {
      if (!res) return;
      state = { ...state, ...res, busy: false };
      syncUi();
    })
    .catch(() => {
      host.hidden = true;
    });

  host.append(h('div', { class: 'sheet-row-switch' }, h('div', { class: 'sheet-row-copy' }, labelEl, subEl), toggleBtn), graceRow);
  return host;
}

function buildDebugServerRow() {
  if (!isDebugBuild()) return null;
  const currentUrl = getConfiguredServerUrl() || '默认（应用内代理）';
  return h(
    'button',
    {
      type: 'button',
      class: 'sheet-row-btn',
      onClick: () => {
        haptic('light');
        openServerConfig();
      },
    },
    h('span', { text: '开发调试连接' }),
    h('small', { text: currentUrl }),
  );
}

export function openSettings({ onSignOut, onAccountDeleted } = {}) {
  const current = initialEdition({ preferredEdition: store.get().edition });
  const atmospheres = ['cotton', 'hobonichi', 'museglass'];
  const cards = atmospheres.map((e) =>
    h(
      'button',
      {
        type: 'button',
        class: ['edition-chip-card', e === current && 'is-active'],
        dataset: { edition: e },
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
      h('span', { class: ['edition-swatch', e], dataset: { ed: e }, 'aria-hidden': 'true' }),
      h('span', { class: 'edition-name', text: EDITION_LABEL[e] }),
    ),
  );

  const handleSignOut = async () => {
    const pendingCount = (store.get().pending || []).length;
    if (pendingCount > 0) {
      const choice = await mwNativeUI.showSheet({
        style: 'actionSheet',
        title: `还有 ${pendingCount} 条本地手帐尚未同步`,
        message: '建议先连网同步后再退出，以免未上传内容丢失',
        items: [
          { id: 'signout', label: '仍要退出登录', role: 'destructive' },
          { id: 'cancel', label: '取消', role: 'cancel' },
        ],
      });
      if (choice?.selectedId !== 'signout') return;
    }
    haptic('warning');
    closeSheet({ silentHaptic: true });
    await api.logout();
    onSignOut?.({ explicit: true });
  };

  openSheet({
    title: '我的设置',
    body: [
      h('div', { class: 'sheet-section settings-group' }, h('p', { class: 'sheet-section-title settings-group-title', text: '纸面氛围' }), h('div', { class: 'settings-card' }, h('div', { class: 'edition-grid', role: 'radiogroup', 'aria-label': '纸面氛围' }, ...cards))),
      h(
        'div',
        { class: 'sheet-section settings-group' },
        h('p', { class: 'sheet-section-title settings-group-title', text: '数据与隐私' }),
        h(
          'div',
          { class: 'settings-card' },
          buildBiometricsBlock(),
          h(
            'button',
            {
              type: 'button',
              class: 'sheet-row-btn settings-row',
              onClick: () => {
                haptic('light');
                closeSheet({ silentHaptic: true });
                actions.openDataRights('export');
              },
            },
            h('span', { text: '导出全部手帐' }),
            h('small', { text: '生成属于你的完整 .zip 备份包' }),
          ),
        ),
      ),
      h(
        'div',
        { class: 'sheet-section settings-group' },
        h('p', { class: 'sheet-section-title settings-group-title', text: '账号' }),
        h(
          'div',
          { class: 'settings-card' },
          buildDebugServerRow(),
          h(
            'button',
            { type: 'button', class: 'sheet-row-btn settings-row', onClick: handleSignOut },
            h('span', { text: '退出登录' }),
            h('small', { text: '手帐会安全地留在云端' }),
          ),
          h(
            'button',
            {
              type: 'button',
              class: 'sheet-row-btn settings-row is-danger',
              onClick: () => {
                haptic('warning');
                closeSheet({ silentHaptic: true });
                actions.openDataRights('delete', { onAccountDeleted: onAccountDeleted || (() => onSignOut?.({ explicit: true })) });
              },
            },
            h('span', { text: '注销账号并彻底粉碎数据' }),
            h('small', { text: '不可撤销地销毁全部手帐与云端密钥' }),
          ),
        ),
      ),
    ],
  });
}
