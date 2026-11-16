// e2e_smoke.mjs — P0 "stranger" flow against a real backend (dev, fake AI provider).
// login → onboarding sentence → TA's first page → arrow → Cupid's first reply (citation) →
// journal → text capture → undo → pencil draft → chat (delta + citation) → citation jump →
// tier change (+ silver cloak) → settings paper → crystal → directory → reload.
//
// Usage: BASE=http://127.0.0.1:8765 PLAYWRIGHT=/path/to/node_modules/playwright/index.mjs node web/tests/e2e_smoke.mjs
import { mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const BASE = process.env.BASE || 'http://127.0.0.1:8765';
const SHOTS = process.env.SHOTS || path.join(here, 'screenshots');
mkdirSync(SHOTS, { recursive: true });
const { chromium } = await import(process.env.PLAYWRIGHT || 'playwright');

const browser = await chromium.launch({ args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'] });
const context = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, locale: 'zh-CN', permissions: ['microphone', 'clipboard-read', 'clipboard-write'] });
const page = await context.newPage();
const problems = [];
page.on('pageerror', (e) => problems.push(`pageerror: ${e.message}`));
page.on('console', (m) => m.type() === 'error' && problems.push(`console: ${m.text()}`));
page.on('response', (r) => r.status() >= 500 && problems.push(`HTTP ${r.status()} ${r.url()}`));

let n = 0;
const steps = [];
async function shot(name) {
  n += 1;
  const file = path.join(SHOTS, `${String(n).padStart(2, '0')}_${name}.png`);
  await page.waitForTimeout(450);
  await page.screenshot({ path: file });
  return file;
}
async function step(name, fn) {
  const t0 = Date.now();
  try {
    await fn();
    steps.push(`✔ ${name} (${Date.now() - t0} ms)`);
  } catch (err) {
    steps.push(`✘ ${name}: ${err.message.split('\n')[0]}`);
    await shot(`FAIL_${name.replace(/\W+/g, '_')}`).catch(() => {});
    throw err;
  }
}
const visibleText = (sel) => page.locator(sel).first().innerText();

try {
  await step('sign in with email code', async () => {
    await page.goto(BASE + '/');
    await page.waitForSelector('#authEmail', { state: 'visible' });
    await shot('auth');
    await page.fill('#authEmail', `stranger${Date.now()}@example.com`);
    await page.click('#authSendCode');
    await page.waitForFunction(() => document.querySelector('#authCode')?.value?.length === 6);
    await page.click('#authVerify');
  });

  await step('onboarding: one sentence', async () => {
    await page.waitForSelector('#onboarding.is-active #onbText');
    await shot('onboarding_question');
    await page.fill('#onbText', '妈妈最近总说膝盖疼');
    await page.click('#onbSend');
    await page.waitForSelector('.onb-arrows .onb-arrow-card');
    await shot('onboarding_first_page');
  });

  await step('onboarding: pick gold arrow → Cupid first reply cites the sentence', async () => {
    await page.click('.onb-arrow-card[data-tier="gold"]');
    await page.waitForSelector('.onb-reply .cite-pill');
    await page.waitForFunction(() => (document.querySelector('.onb-reply .ai-text')?.textContent || '').length > 4);
    await shot('onboarding_cupid_reply');
    await page.click('#onbEnter');
    await page.waitForSelector('#onboarding', { state: 'hidden' });
    await page.waitForSelector('.mat-card[data-material-id]');
    const body = await visibleText('.mat-card[data-material-id] .mat-body');
    if (!body.includes('膝盖')) throw new Error('first sentence not on the page');
    await shot('journal_first_page');
  });

  await step('text capture lands on the page with an undo toast', async () => {
    // no microphone gesture in headless → tap the orb briefly, then use the typing fallback
    await page.click('#voiceOrb');
    const typed = await page.waitForSelector('#captureText', { timeout: 2500 }).catch(() => null);
    if (!typed) {
      await page.waitForSelector('#holdHint.is-visible');
      await page.click('#holdHintType');
      await page.waitForSelector('#captureText');
    }
    await page.fill('#captureText', '她不吃香菜，生日是5月12日');
    await page.click('#captureSend');
    await page.waitForSelector('.mw-toast .mw-toast-undo');
    await page.waitForFunction(() => document.querySelectorAll('.mat-card[data-material-id]').length >= 2);
    await shot('capture_undo_toast');
  });

  await step('undo removes it', async () => {
    await page.click('.mw-toast .mw-toast-undo');
    await page.waitForFunction(() => /撤回/.test(document.querySelector('#toastHost')?.textContent || ''));
    await page.waitForFunction(() => document.querySelectorAll('.mat-card[data-material-id]').length === 1);
    await shot('capture_undone');
  });

  await step('pencil draft shows in place with a glance chip', async () => {
    await page.click('#voiceOrb');
    const typed = await page.waitForSelector('#captureText', { timeout: 2500 }).catch(() => null);
    if (!typed) {
      await page.waitForSelector('#holdHint.is-visible');
      await page.click('#holdHintType');
    }
    await page.fill('#captureText', '她不吃香菜，生日是5月12日');
    await page.keyboard.press('Enter');
    await page.waitForSelector('.mat-card.is-pencil .glance-chip');
    await page.waitForTimeout(5200); // let the undo window pass
    await page.click('.mat-card.is-pencil .glance-chip');
    await page.waitForSelector('.glance-actions');
    await shot('pencil_draft_glance');
    await page.click('.glance-actions .glance-btn');
    await page.waitForFunction(() => !document.querySelector('.mat-card.is-pencil'));
  });

  await step('voice hold: short tap shows hint without sending; >=1.5s hold ingests voice', async () => {
    let voicePosts = 0;
    const onReq = (req) => {
      if (req.method() === 'POST' && req.url().includes('/v1/voice/ingest')) voicePosts += 1;
    };
    page.on('request', onReq);
    try {
      const box = await page.locator('#voiceOrb').boundingBox();
      if (!box) throw new Error('#voiceOrb missing bounding box');
      const cx = box.x + box.width / 2;
      const cy = box.y + box.height / 2;
      // Short tap (<300ms): hint appears, zero network request
      await page.mouse.move(cx, cy);
      await page.mouse.down();
      await page.waitForTimeout(80);
      await page.mouse.up();
      await page.waitForSelector('#holdHint.is-visible');
      await page.waitForTimeout(350);
      if (voicePosts !== 0) throw new Error(`short tap triggered ${voicePosts} voice upload(s)`);

      // Long hold (>=1.5s): /v1/voice/ingest -> voice slip lands on page
      const beforeCards = await page.locator('.mat-card[data-material-id]').count();
      const ingestPromise = page.waitForResponse(
        (r) => r.url().includes('/v1/voice/ingest') && r.request().method() === 'POST',
      );
      await page.mouse.move(cx, cy);
      await page.mouse.down();
      await page.waitForSelector('#voiceStage.is-active');
      await page.waitForTimeout(1600);
      await page.mouse.up();
      const ingestRes = await ingestPromise;
      if (ingestRes.status() !== 200) throw new Error(`/v1/voice/ingest returned ${ingestRes.status()}`);
      if (voicePosts !== 1) throw new Error(`expected 1 voice upload, got ${voicePosts}`);
      await page.waitForSelector('.mat-card[data-material-id] .voice-charm');
      await page.waitForFunction((prev) => document.querySelectorAll('.mat-card[data-material-id]').length > prev, beforeCards);
      await shot('voice_capture');
    } finally {
      page.off('request', onReq);
    }
  });

  await step('chat with Cupid: streamed reply + citation pill', async () => {
    await page.waitForFunction(() => !document.querySelector('#toastHost .mw-toast'));
    await page.click('#aiOrb');
    await page.waitForSelector('#chat.is-active .chat-text-input');
    await page.fill('.chat-text-input', '妈妈膝盖不舒服，我能做点什么？');
    await page.keyboard.press('Enter');
    await page.waitForSelector('#chat .chat-bubble.user');
    await page.waitForSelector('#chat .chat-bubble.ai:last-of-type .cite-pill, #chat .cite-pill');
    await page.waitForFunction(() => [...document.querySelectorAll('#chat .ai-text')].some((n) => n.textContent.length > 4));
    const label = await visibleText('#chat .cite-pill .cite-label');
    if (/[0-9a-f]{8}-[0-9a-f]{4}/i.test(label)) throw new Error('citation shows an id');
    await shot('chat_citation');
  });

  await step('citation jumps back to the page', async () => {
    await page.locator('#chat .cite-pill').last().click();
    await page.waitForSelector('#chat', { state: 'hidden' });
    await page.waitForSelector('.mat-card.is-flash', { timeout: 3000 }).catch(() => {});
    await shot('citation_jump');
  });

  await step('keyboard & silver cloak: capsule/popover Esc + change arrow to silver', async () => {
    await page.focus('#capsule');
    await page.keyboard.press('Enter');
    await page.waitForSelector('#capsule.is-expanded');
    await page.keyboard.press('Escape');
    await page.waitForFunction(() => !document.querySelector('#capsule.is-expanded'));

    await page.click('.pinned-arrow-tack-btn');
    await page.waitForSelector('.arrow-selector-popover.is-open');
    await shot('arrow_popover');
    await page.keyboard.press('Escape');
    await page.waitForFunction(() => !document.querySelector('.arrow-selector-popover.is-open'));

    await page.click('.pinned-arrow-tack-btn');
    await page.waitForSelector('.arrow-selector-popover.is-open');
    await page.click('.arrow-choice-item:nth-child(2)');
    await page.waitForFunction(() => document.querySelector('#app').dataset.arrowTier === 'silver');
    await page.waitForSelector('.mw-toast .mw-toast-undo');
    await page.waitForSelector('.capsule-active-token.is-cloaked');
    await shot('silver_tier');
  });

  await step('settings: switch paper to parchment', async () => {
    await page.waitForFunction(() => !document.querySelector('#toastHost .mw-toast'));
    await page.click('#settingsBtn');
    await page.waitForSelector('.edition-chip-card');
    await shot('settings');
    await page.click('.edition-chip-card:nth-child(3)');
    await page.waitForFunction(() => document.querySelector('#app').dataset.journalEdition === 'parchment');
    await page.keyboard.press('Escape');
    await shot('parchment');
  });

  await step('crystal → 记繁琐 directory', async () => {
    await page.click('.pinned-arrow-tack-btn');
    await page.click('.arrow-choice-item:nth-child(3)');
    await page.waitForSelector('#backToCasual:not([hidden])');
    await page.click('#backToCasual');
    await page.waitForFunction(() => document.querySelector('#app').dataset.viewMode === 'casual-directory');
    await page.waitForSelector('.casual-folio-row');
    await shot('directory');
  });

  await step('reload keeps you signed in and on your page', async () => {
    await page.locator('.casual-folio-row').first().click();
    await page.waitForFunction(() => document.querySelector('#app').dataset.viewMode === 'person-journal');
    await page.reload();
    await page.waitForSelector('.mat-card[data-material-id]');
    if (await page.locator('#authScreen').isVisible()) throw new Error('signed out after reload');
    if ((await page.locator('#app').getAttribute('data-journal-edition')) !== 'parchment') throw new Error('paper not persisted');
    await shot('reload');
  });
} catch {
  /* reported below */
} finally {
  await browser.close();
  console.log(steps.join('\n'));
  if (problems.length) console.log('\nProblems:\n' + [...new Set(problems)].join('\n'));
  console.log(`\nscreenshots: ${SHOTS}`);
  const failed = steps.some((s) => s.startsWith('✘')) || problems.length > 0;
  process.exit(failed ? 1 : 0);
}
