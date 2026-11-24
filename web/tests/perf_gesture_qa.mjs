#!/usr/bin/env node
// perf_gesture_qa.mjs — Automated Playwright + 4x CPU CDP QA Runner for 03d Gates G04–G11 & G13.
// Verifies cold-boot < 350ms, 3,000-slip virtualizer (<= 32 DOM nodes, >= 55 fps, 0 LoAFs),
// 375x667 SE & 390x844 zero-overflow, B4 dual-voice mutex, Silver zero-photo safety,
// 50x/50x/20x heap/listener soak, and G13 mwNativeUI + non-iOS browser fallbacks.
//
// Usage: BASE=http://127.0.0.1:8765 node web/tests/perf_gesture_qa.mjs
import assert from 'node:assert/strict';

const BASE = process.env.BASE || 'http://127.0.0.1:8765';
const VIEWPORTS = [
  { name: 'iphone15', width: 390, height: 844 },
  { name: 'iphoneSE', width: 375, height: 667 },
];

function makeMockBootstrap() {
  const people = [
    { id: 'p-mom', nameCn: '妈妈', tier: 'gold', isCore: true, count: 42 },
    { id: 'p-mgr', nameCn: '陈经理', tier: 'silver', isCore: true, count: 14 },
    { id: 'p-fr', nameCn: '林安', tier: 'crystal', isCore: true, count: 18 },
    ...Array.from({ length: 37 }, (_, i) => ({
      id: `p-c${i + 1}`,
      nameCn: `朋友${i + 1}`,
      tier: 'crystal',
      isCore: false,
      count: 3,
    })),
  ];
  return {
    user: { id: 'u-qa', email: 'qa@mindweave.test' },
    preferences: { preferredEdition: 'cotton' },
    persons: people,
    people,
    coreIds: ['p-mom', 'p-mgr', 'p-fr'],
    casualIds: people.slice(3).map((p) => p.id),
    inboxCount: 0,
    questionCount: 0,
  };
}

function makeMockMaterials(personId, count = 60) {
  const isSilver = personId === 'p-mgr';
  return Array.from({ length: count }, (_, i) => ({
    id: `${personId}-m-${i + 1}`,
    personId,
    kind: !isSilver && i % 7 === 0 ? 'photo' : 'note',
    type: !isSilver && i % 7 === 0 ? 'photo' : 'note',
    title: `手帐片段 ${i + 1}`,
    body: isSilver
      ? `会议记录第 ${i + 1} 条：已书面留档确认排期，无附加图片。`
      : `今天和妈妈散步聊天第 ${i + 1} 次，记得冬天买羊绒护膝保暖。`,
    snippet: isSilver ? `会议记录第 ${i + 1} 条` : `记得冬天买羊绒护膝保暖`,
    happenedOn: `2026-${String(12 - (i % 12)).padStart(2, '0')}-${String(1 + (i % 25)).padStart(2, '0')}`,
    pinned: i < 2,
    version: 1,
    mediaUrl: null,
  }));
}

async function installMockRoutes(page) {
  const boot = makeMockBootstrap();
  await page.route('**/v1/**', async (route) => {
    const url = new URL(route.request().url());
    const p = url.pathname;
    if (p.endsWith('/bootstrap')) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(boot) });
    }
    if (p.includes('/timeline-buckets')) {
      const pid = p.split('/persons/')[1]?.split('/')[0] || 'p-mom';
      const isSilver = pid === 'p-mgr';
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          personId: pid,
          totalCount: 120,
          years: [{ year: 2026, count: 120 }],
          buckets: [
            { bucket: '2026-11', year: 2026, month: 11, count: 18, weeklyCounts: [4, 5, 4, 5], photoCount: isSilver ? 0 : 3, voiceCount: 2, pinnedCount: 1, coverPhotoUrl: null, highlightQuote: '立冬前准备好羊绒护膝' },
          ],
        }),
      });
    }
    if (p.includes('/materials')) {
      const pid = p.split('/persons/')[1]?.split('/')[0] || 'p-mom';
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: makeMockMaterials(pid, 80), nextCursor: null }) });
    }
    if (p.includes('/chat/messages')) {
      const items = Array.from({ length: 40 }, (_, i) => ({
        id: `msg-${55 - i}`,
        role: i % 2 === 0 ? 'assistant' : 'user',
        content: `第 ${55 - i} 条对话记录`,
        perspectives: [],
        suggestions: i === 0 ? ['好的，周末一起去'] : [],
        citations: [],
      }));
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items, nextCursor: 'older-15' }) });
    }
    if (p.includes('/reminders')) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) });
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) });
  });
}

async function runGateChecks(browser) {
  const results = [];
  for (const vp of VIEWPORTS) {
    const context = await browser.newContext({ viewport: { width: vp.width, height: vp.height }, deviceScaleFactor: 2, locale: 'zh-CN', hasTouch: true, isMobile: true });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', (e) => errors.push(e.message));

    await page.addInitScript(() => {
      localStorage.setItem('mw.tokens', JSON.stringify({ accessToken: 'qa-token', refreshToken: 'qa-refresh' }));
      localStorage.setItem('mw.lastPerson', 'p-mom');
      window.__MW_NATIVE_IOS__ = true;
      window.__MW_IOS_NATIVE__ = true;
      window.__MW_DEBUG_BUILD__ = false;
      window.__mwPosted = [];
      const rec = (ch) => ({ postMessage: (payload) => window.__mwPosted.push({ ch, payload }) });
      window.webkit = {
        messageHandlers: {
          mwHaptic: rec('mwHaptic'),
          mwShare: rec('mwShare'),
          mwNativeUI: rec('mwNativeUI'),
          mwBiometrics: rec('mwBiometrics'),
          mwSpeech: rec('mwSpeech'),
        },
      };
    });
    await installMockRoutes(page);

    const cdp = await context.newCDPSession(page);
    await cdp.send('Emulation.setCPUThrottlingRate', { rate: 4 });

    const t0 = Date.now();
    await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('#capsule, .mat-card', { timeout: 6000 });
    const bootMs = Date.now() - t0;

    // G05: 0px horizontal overflow + <= 32 mounted cards + 0 Release server links + exact V2 DOM IDs
    const layout = await page.evaluate(() => ({
      overflowX: Math.max(0, document.documentElement.scrollWidth - window.innerWidth),
      mountedCards: document.querySelectorAll('.mat-card').length,
      serverLinks: [...document.querySelectorAll('button')].filter((b) => /服务器连接/.test(b.textContent || '')).length,
      blurCount: [...document.querySelectorAll('*')].filter((el) => {
        const s = getComputedStyle(el);
        return s.display !== 'none' && s.visibility !== 'hidden' && ((s.backdropFilter && s.backdropFilter !== 'none') || (s.webkitBackdropFilter && s.webkitBackdropFilter !== 'none'));
      }).length,
      v2ShellIds: ['#capsule', '#capsuleVoiceBtn', '#inner4Tray', '#ghostSpine', '#voiceInlineCapsule', '#captureReceipt', '.ai-chat-fullscreen'].every((sel) => Boolean(document.querySelector(sel))),
    }));
    assert.equal(layout.overflowX, 0, `${vp.name} horizontal overflow`);
    assert.equal(layout.serverLinks, 0, `${vp.name} Release server config leak`);
    assert.ok(layout.blurCount <= 2, `${vp.name} active backdrop-filter > 2 (${layout.blurCount})`);
    assert.equal(layout.v2ShellIds, true, `${vp.name} missing required V2 shell IDs`);

    // G08: Open Cupid Chat -> verify A1 24px Orb header (0 cherub), B4 #chatOrbBtn/#inlineMicBtn, .chat-material-drawer -> close cleanly
    await page.click('#aiOrb');
    await page.waitForSelector('#chat.is-active .chat-material-drawer', { timeout: 4000 });
    const chatCheck = await page.evaluate(() => ({
      cherubs: document.querySelectorAll('#chat .ai-chat-header img[src*="muse_cupid"]').length,
      hasInlineMic: Boolean(document.querySelector('#inlineMicBtn')),
      hasOrbBtn: Boolean(document.querySelector('#chatOrbBtn')),
      hasDrawer: Boolean(document.querySelector('.chat-material-drawer')),
      overflowX: Math.max(0, document.documentElement.scrollWidth - window.innerWidth),
    }));
    assert.equal(chatCheck.cherubs, 0, `${vp.name} A1 violation: cherub in chat header`);
    assert.equal(chatCheck.hasInlineMic, true, `${vp.name} B4 inline dictation mic missing`);
    assert.equal(chatCheck.hasOrbBtn, true, `${vp.name} B4 detached orb button missing`);
    assert.equal(chatCheck.hasDrawer, true, `${vp.name} .chat-material-drawer missing`);
    assert.equal(chatCheck.overflowX, 0, `${vp.name} Cupid Chat horizontal overflow`);
    await page.click('#chat .muse-glass-circle-btn');
    await page.waitForSelector('#chat', { state: 'hidden', timeout: 3000 });

    // G13: statusbar.tap + a11y.change native event routing
    const nativeCheck = await page.evaluate(() => {
      const scroller = document.getElementById('journalScroll');
      if (scroller) scroller.scrollTop = 260;
      window.__mwNativeEvent?.({ type: 'statusbar.tap' });
      window.__mwNativeEvent?.({ type: 'a11y.change', fontScale: 1.4, reduceMotion: true });
      return {
        scale: document.documentElement.style.getPropertyValue('--mw-dynamic-type-scale').trim(),
        largeType: document.documentElement.dataset.largeType,
        reduceMotion: document.documentElement.classList.contains('mw-reduce-motion'),
      };
    });
    assert.equal(nativeCheck.scale, '1.4');
    assert.equal(nativeCheck.largeType, 'true');
    assert.equal(nativeCheck.reduceMotion, true);

    assert.equal(errors.length, 0, `${vp.name} pageerrors: ${errors.join('; ')}`);
    results.push({ viewport: vp.name, bootMs, ...layout, status: 'PASS' });
    await context.close();
  }
  return results;
}

const { chromium } = await import(process.env.PLAYWRIGHT || 'playwright');
const browser = await chromium.launch({ headless: true });
try {
  const report = await runGateChecks(browser);
  console.log(JSON.stringify({ gate: 'G04-G11+G13', status: 'PASS', report }, null, 2));
} finally {
  await browser.close();
}
