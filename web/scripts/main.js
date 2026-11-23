// main.js — boot: sign in → bootstrap → first-time ritual or the last page you were on.
// Wires the cross-view action registry and registers the offline shell.
import { $, h } from './dom.js';
import { initNativeBridge, haptic, isNativeIos, openServerConfig } from './native-bridge.js';
import { api, client } from './api/endpoints.js';
import { store, applyBootstrap, isEmptyAccount } from './store.js';
import { actions } from './actions.js';
import { showAuth } from './views/auth.js';
import { initJournal, openPerson, reloadMaterials } from './views/journal.js';
import { initDock } from './views/dock.js';
import { initCasual, openDirectory } from './views/casual.js';
import { initVoice } from './views/voice.js';
import { initCapture, openTextCapture, flyAvatars } from './views/capture.js';
import { openChat, closeChat } from './views/chat.js';
import { crisisCard } from './views/chat-render.js';
import { openSettings, applyEdition, initialEdition } from './views/settings.js';
import { runOnboarding } from './views/onboarding.js';
import { openSheet, closeSheet } from './views/sheet.js';
import { whisper } from './views/toast.js';

const LAST_PERSON_KEY = 'mw.lastPerson';
let captureStarted = false;

async function refreshBootstrap() {
  const data = await api.bootstrap();
  applyBootstrap(data);
  const s = store.get();
  if (s.activePersonId && !s.people[s.activePersonId]) store.set({ activePersonId: null });
  return data;
}

function showCrisis(card) {
  openSheet({ label: '关心你', body: crisisCard(card, { onContinue: () => closeSheet() }) });
}

Object.assign(actions, {
  openPerson,
  openDirectory,
  openChat,
  closeChat,
  openTextCapture,
  reloadMaterials,
  refreshBootstrap: () => refreshBootstrap().catch(() => null),
  showCrisis,
  flyAvatars,
});

function landingPerson() {
  const s = store.get();
  let last = null;
  try {
    last = localStorage.getItem(LAST_PERSON_KEY);
  } catch {
    /* ignore */
  }
  if (last && s.people[last]) return last;
  return s.coreIds[0] || null;
}

function resetState() {
  store.set({ ready: false, people: {}, coreIds: [], casualIds: [], view: 'journal', activePersonId: null, materials: {}, pending: [], inboxCount: 0, questionCount: 0 });
  try {
    localStorage.removeItem(LAST_PERSON_KEY);
  } catch {
    /* ignore */
  }
}

async function signOut(message) {
  closeChat();
  closeSheet();
  resetState();
  if (message) whisper(message, 3000);
  await showAuth();
  await enterApp();
}

function bootError(err) {
  const veil = $('#bootVeil');
  veil.classList.remove('is-gone');
  const nodes = [
    h('p', { text: err?.message || '心络这边出了点小状况，稍后再试' }),
    h('button', { type: 'button', class: 'onb-enter', text: '再试一次', onClick: () => { haptic('light'); enterApp(); } }),
  ];
  if (isNativeIos()) {
    nodes.push(
      h('button', {
        type: 'button',
        class: 'boot-server-link',
        text: '服务器连接',
        onClick: () => {
          haptic('light');
          openServerConfig();
        },
      }),
    );
  }
  veil.replaceChildren(...nodes);
}

async function enterApp() {
  let data;
  try {
    data = await refreshBootstrap();
  } catch (err) {
    if (err?.status === 401) return; // onAuthLost handles it
    bootError(err);
    return;
  }
  $('#bootVeil').classList.add('is-gone');
  applyEdition(initialEdition(data?.preferences));
  if (!captureStarted) {
    captureStarted = true;
    initCapture();
  }
  if (isEmptyAccount()) {
    const pid = await runOnboarding();
    if (pid && store.get().activePersonId !== pid) openPerson(pid);
    return;
  }
  const pid = landingPerson();
  if (pid) openPerson(pid);
  else openDirectory();
}

function registerServiceWorker() {
  if (!('serviceWorker' in navigator) || location.protocol === 'file:' || location.protocol === 'mindweave:') return;
  window.addEventListener('load', () => navigator.serviceWorker.register('./sw.js').catch(() => {}));
}

async function boot() {
  initNativeBridge();
  client.setOnAuthLost(() => signOut('登录已过期，请重新登录'));
  initJournal();
  initDock();
  initCasual();
  initVoice();
  $('#settingsBtn').addEventListener('click', () => {
    haptic('light');
    openSettings({ onSignOut: () => signOut() });
  });
  registerServiceWorker();
  if (!client.isLoggedIn()) {
    $('#bootVeil').classList.add('is-gone');
    await showAuth();
  }
  await enterApp();
}

boot();
