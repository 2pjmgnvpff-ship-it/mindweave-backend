// main.js — < 350ms 10-module critical boot, SWR snapshot, dynamic import() splitting,
// async chat.css/sheets.css loading, global 01m+02g native iOS routers (statusbar.tap,
// a11y.change, notification.*, shortcut.action), and clean signOut hygiene.
import { $, h, ensureStyle } from './dom.js';
import {
  initNativeBridge,
  haptic,
  isDebugBuild,
  isNativeIos,
  openServerConfig,
  onNativeEvent,
  mwBiometrics,
  prefersReducedMotion,
} from './native-bridge.js';
import { api, client } from './api/endpoints.js';
import { store, applyBootstrap, isEmptyAccount, resetRuntimeCaches } from './store.js';
import { actions } from './actions.js';
import { initOrbVisibilityGuard } from './lib/orb.js';
import { initKeyboardViewportBridge } from './lib/swipe-arbiter.js';
import { initJournal, openPerson, reloadMaterials, loadMaterials, resetJournalCaches, scrollJournalToTop } from './views/journal.js';
import { initDock } from './views/dock.js';
import { initVoice } from './views/voice.js';
import { whisper } from './views/toast.js';

const LAST_PERSON_KEY = 'mw.lastPerson';
const SWR_BOOT_KEY = 'mw.swrBoot';
let captureStarted = false;
let nativeRoutersBound = false;

const loadWithStyles = async (styles, importer) => {
  const [, mod] = await Promise.all([
    Promise.all(styles.map((href) => ensureStyle?.(href))),
    importer(),
  ]);
  return mod;
};

const loadChatMod = () => loadWithStyles(['./styles/chat.css', './styles/sheets.css'], () => import('./views/chat.js'));
const loadSheetMod = () => loadWithStyles(['./styles/sheets.css'], () => import('./views/sheet.js'));
const loadAuthMod = () => loadWithStyles(['./styles/sheets.css'], () => import('./views/auth.js'));
const loadOnbMod = () => loadWithStyles(['./styles/sheets.css', './styles/chat.css'], () => import('./views/onboarding.js'));
const loadSettingsMod = () => loadWithStyles(['./styles/sheets.css'], () => import('./views/settings.js'));
const loadDataRightsMod = () => loadWithStyles(['./styles/sheets.css'], () => import('./views/data-rights-sheet.js'));
const loadCrisisMod = () => loadWithStyles(['./styles/sheets.css'], () => import('./views/crisis.js'));
const loadPeopleMod = () => loadWithStyles(['./styles/sheets.css'], () => import('./views/people-sheet.js'));
const loadArchiveMod = () => loadWithStyles(['./styles/sheets.css'], () => import('./views/archive-history-sheet.js'));
const loadPeekMod = () => loadWithStyles(['./styles/sheets.css'], () => import('./views/peek-date.js'));
const loadCaptureMod = () => loadWithStyles(['./styles/sheets.css'], () => import('./views/capture.js'));
const loadReceiptMod = () => import('./views/capture-receipt.js');

function readLastPerson() {
  try {
    return localStorage.getItem(LAST_PERSON_KEY) || null;
  } catch {
    return null;
  }
}

function hydrateSwrSnapshot() {
  try {
    const raw = localStorage.getItem(SWR_BOOT_KEY);
    if (!raw) return null;
    const snap = JSON.parse(raw);
    if (snap && typeof snap === 'object' && ((snap.people && typeof snap.people === 'object') || Array.isArray(snap.persons))) {
      applyBootstrap(snap);
      return snap;
    }
  } catch {
    /* ignore corrupt snapshot */
  }
  return null;
}

function saveSwrSnapshot(data) {
  try {
    localStorage.setItem(SWR_BOOT_KEY, JSON.stringify(data));
  } catch {
    /* quota / private mode */
  }
}

async function refreshBootstrap() {
  const data = await api.bootstrap();
  applyBootstrap(data);
  saveSwrSnapshot(data);
  const s = store.get();
  if (s.activePersonId && !s.people[s.activePersonId]) store.set({ activePersonId: null });
  return data;
}

function landingPerson() {
  const s = store.get();
  const last = readLastPerson();
  if (last && s.people[last]) return last;
  return s.coreIds[0] || s.casualIds[0] || null;
}

function bindActions() {
  Object.assign(actions, {
    openPerson,
    reloadMaterials,
    refreshBootstrap: () => refreshBootstrap().catch(() => null),
    openChat: (pid, opts) => loadChatMod().then((m) => m.openChat(pid, opts)),
    closeChat: (opts) => import('./views/chat.js').then((m) => m.closeChat(opts)).catch(() => {}),
    openPeopleSheet: (opts) => loadPeopleMod().then((m) => m.openPeopleSheet(opts)),
    openDirectory: (opts) => loadPeopleMod().then((m) => m.openPeopleSheet(opts)),
    openArchiveHistory: (opts) => loadArchiveMod().then((m) => m.openArchiveHistory(opts)),
    openPeek: (mid, person, opts) => loadPeekMod().then((m) => (m.openPeek || m.openPeekOverlay)?.(mid, person, opts)),
    openCaptureReceipt: (res, meta) => loadReceiptMod().then((m) => (m.showCaptureReceipt || m.openCaptureReceipt)?.(res, meta)),
    openTextCapture: (opts) => loadCaptureMod().then((m) => m.openTextCapture(opts)),
    flyAvatars: (splits) => loadCaptureMod().then((m) => m.flyAvatars?.(splits)),
    openSettings: () => loadSettingsMod().then((m) => m.openSettings({ onSignOut: (o) => signOut(o), onAccountDeleted: () => signOut({ explicit: true }) })),
    openDataRights: (mode, opts) => loadDataRightsMod().then((m) => m.openDataRights(mode, { onAccountDeleted: () => signOut({ explicit: true }), ...opts })),
    openCrisisCard: (card, opts) => loadCrisisMod().then((m) => m.openCrisisCard(card, opts)),
    showCrisis: (card) => loadCrisisMod().then((m) => m.openCrisisCard(card)),
  });
}

/** Global iOS-Native event routers (01m × 02g × 03e §2.5). */
function bindNativeRouters() {
  if (nativeRoutersBound) return;
  nativeRoutersBound = true;

  // Status-Bar Tap-to-Top (`statusbar.tap` — native-bridge.js also aliases legacy `ui.scrollToTop`)
  onNativeEvent?.('statusbar.tap', () => {
    const reduced = prefersReducedMotion() || document.documentElement.classList.contains('mw-reduce-motion');
    const sheetBody = $('.mw-sheet-layer.is-open .sheet-body');
    if (sheetBody) {
      const wasDeep = sheetBody.scrollTop > 120;
      sheetBody.scrollTo({ top: 0, behavior: reduced ? 'auto' : 'smooth' });
      if (wasDeep) haptic('soft', { intensity: 0.32 });
      return;
    }
    const chatBox = $('#chat.is-active .ai-chat-messages');
    if (chatBox) {
      const wasDeep = chatBox.scrollTop > 120;
      import('./views/chat.js').then((m) => m.scrollChatToTop?.(!reduced)).catch(() => chatBox.scrollTo({ top: 0, behavior: reduced ? 'auto' : 'smooth' }));
      if (wasDeep) haptic('soft', { intensity: 0.32 });
      return;
    }
    const scroller = $('#journalScroll');
    const wasDeep = (scroller?.scrollTop || 0) > 120;
    if (typeof scrollJournalToTop === 'function') scrollJournalToTop(!reduced);
    else scroller?.scrollTo({ top: 0, behavior: reduced ? 'auto' : 'smooth' });
    if (wasDeep) haptic('soft', { intensity: 0.32 });
  });

  // Live UIAccessibility Dynamic Type & Reduce Motion sync
  onNativeEvent?.('a11y.change', (ev) => {
    const root = document.documentElement;
    if (typeof ev?.fontScale === 'number' && Number.isFinite(ev.fontScale)) {
      const isLarge = ev.fontScale >= 1.3;
      root.style.setProperty('--mw-dynamic-type-scale', String(ev.fontScale));
      root.classList.toggle('mw-large-type', isLarge);
      root.dataset.largeType = String(isLarge);
    }
    if (typeof ev?.reduceMotion === 'boolean') {
      root.classList.toggle('mw-reduce-motion', ev.reduceMotion);
    }
  });

  // Calm Lock-Screen Notification tap & background actions
  onNativeEvent?.('notification.opened', (ev) => {
    if (ev?.personId && store.get().people[ev.personId]) openPerson(ev.personId);
  });
  onNativeEvent?.('notification.action', (ev) => {
    if (!ev?.reminderId) return;
    if (ev.actionId === 'MW_ACTION_DONE' || ev.actionId === 'MARK_DONE') {
      api.markReminderDone?.(ev.reminderId).catch(() => {});
    }
  });

  // Home Screen Quick Actions (V2.1 ready)
  onNativeEvent?.('shortcut.action', (ev) => {
    const kind = String(ev?.shortcut || ev?.action || '');
    if (kind === 'cupid') actions.openChat();
    else if (kind === 'archives') actions.openArchiveHistory();
    else if (kind === 'capture') actions.openTextCapture();
  });
}

function resetState({ explicit = false } = {}) {
  resetRuntimeCaches?.();
  store.set({
    ready: false,
    people: {},
    coreIds: [],
    casualIds: [],
    view: 'journal',
    activePersonId: null,
    materials: {},
    bucketsByPerson: {},
    chatDraftsByPerson: {},
    silverVeilByPerson: {},
    silverSessionNoticed: false,
    pending: explicit ? [] : store.get().pending || [],
    inboxCount: 0,
    questionCount: 0,
  });
  try {
    localStorage.removeItem(LAST_PERSON_KEY);
    localStorage.removeItem(SWR_BOOT_KEY);
    if (explicit) localStorage.removeItem('mw.lastEmail');
  } catch {
    /* ignore */
  }
}

async function signOut({ explicit = false, message = '' } = {}) {
  actions.stopActiveAudio?.();
  await actions.closeChat().catch?.(() => {});
  loadSheetMod().then((m) => m.closeSheet({ silentHaptic: true })).catch(() => {});
  resetJournalCaches?.();
  if (explicit && isNativeIos()) mwBiometrics?.setEnabled?.(false).catch(() => {});
  // On involuntary 401 expiry (`explicit === false`), keep IDB offline drafts intact!
  resetState({ explicit });
  if (message) whisper(message, 3000);
  const { showAuth } = await loadAuthMod();
  await showAuth();
  await enterApp();
}

function bootError(err) {
  const veil = $('#bootVeil');
  if (!veil) return;
  veil.classList.remove('is-gone');
  const nodes = [
    h('p', { text: err?.message || '心络这边出了点小状况，稍后再试' }),
    h('button', { type: 'button', class: 'onb-enter', text: '再试一次', onClick: () => (haptic('light'), enterApp()) }),
  ];
  // Strictly #if DEBUG gated — zero server config UI in Release IPA!
  if (isDebugBuild()) {
    nodes.push(
      h('button', {
        type: 'button',
        class: 'boot-server-link',
        text: '服务器连接',
        onClick: () => (haptic('light'), openServerConfig()),
      }),
    );
  }
  veil.replaceChildren(...nodes);
}

async function enterApp() {
  const lastPid = readLastPerson();
  const swr = hydrateSwrSnapshot();
  if (swr && lastPid && store.get().people[lastPid]) {
    $('#bootVeil')?.classList.add('is-gone');
    openPerson(lastPid);
  }

  let data;
  try {
    // 02d parallel boot: fetch bootstrap + last active person materials simultaneously (0 duplicate fetches)
    const [bootRes] = await Promise.allSettled([
      refreshBootstrap(),
      lastPid ? loadMaterials?.(lastPid).catch(() => null) : Promise.resolve(null),
    ]);
    if (bootRes.status === 'rejected') throw bootRes.reason;
    data = bootRes.value;
  } catch (err) {
    if (err?.status === 401) return;
    if (!swr) bootError(err);
    return;
  }

  $('#bootVeil')?.classList.add('is-gone');
  const settingsMod = await loadSettingsMod();
  settingsMod.applyEdition(settingsMod.initialEdition(data?.preferences));

  if (!captureStarted) {
    captureStarted = true;
    loadCaptureMod().then((m) => m.initCapture()).catch(() => {});
  }
  if (isEmptyAccount()) {
    const { runOnboarding } = await loadOnbMod();
    const pid = await runOnboarding();
    if (pid && store.get().activePersonId !== pid) openPerson(pid);
    return;
  }
  const pid = landingPerson();
  if (pid) openPerson(pid);
  else actions.openPeopleSheet();
}

function registerServiceWorker() {
  if (!('serviceWorker' in navigator) || location.protocol === 'file:' || location.protocol === 'mindweave:') return;
  window.addEventListener('load', () => navigator.serviceWorker.register('./sw.js').catch(() => {}));
}

async function boot() {
  initNativeBridge();
  initOrbVisibilityGuard?.(document.documentElement);
  initKeyboardViewportBridge?.();
  bindActions();
  bindNativeRouters();
  client.setOnAuthLost(() => signOut({ explicit: false, message: '登录已过期，请重新登录' }));
  initJournal();
  initDock();
  initVoice();
  $('#settingsBtn')?.addEventListener('click', () => {
    haptic('light');
    actions.openSettings();
  });
  registerServiceWorker();
  if (!client.isLoggedIn()) {
    const { showAuth } = await loadAuthMod();
    const authPromise = showAuth();
    requestAnimationFrame(() => $('#bootVeil')?.classList.add('is-gone'));
    await authPromise;
    $('#bootVeil')?.classList.remove('is-gone');
  }
  await enterApp();
}

boot();
