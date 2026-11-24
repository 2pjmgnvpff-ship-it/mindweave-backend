// e2e_smoke.mjs — V2 Real-Backend Smoke E2E (synced with 01l copy & V2 Gleb Porcelain DOM).
// Usage: BASE=http://127.0.0.1:8765 PLAYWRIGHT=/path/to/playwright/index.mjs node web/tests/e2e_smoke.mjs
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
  await page.waitForTimeout(350);
  await page.screenshot({ path: file });
  return file;
}
async function step(name, fn) {
  const t0 = Date.now();
  try {
    await fn();
    steps.push(`PASS ${name} (${Date.now() - t0} ms)`);
  } catch (err) {
    steps.push(`FAIL ${name}: ${err.message.split('\n')[0]}`);
    await shot(`FAIL_${name.replace(/\W+/g, '_')}`).catch(() => {});
    throw err;
  }
}

try {
  await step('sign in with single-input 6-slot email OTP', async () => {
    await page.goto(BASE + '/');
    await page.waitForSelector('#authEmail', { state: 'visible' });
    await page.fill('#authEmail', `v2smoke${Date.now()}@example.com`);
    await page.click('#authSendCode');
    await page.waitForFunction(() => document.querySelector('#authCode')?.value?.length === 6);
    await page.click('#authVerify');
  });

  await step('3-step sanctuary onboarding -> Gold tier -> Cupid first reply', async () => {
    await page.waitForSelector('#onboarding.is-active #onbText');
    await page.fill('#onbText', '妈妈最近总说膝盖疼');
    await page.click('#onbSend');
    await page.waitForSelector('.onb-arrows .onb-arrow-card[data-tier="gold"]');
    await page.click('.onb-arrow-card[data-tier="gold"]');
    await page.waitForSelector('#onbEnter');
    await page.click('#onbEnter');
    await page.waitForSelector('#onboarding', { state: 'hidden' });
    await page.waitForSelector('.mat-card');
  });

  await step('Cupid Chat A1 Orb header + B4 dual-voice composer + citation', async () => {
    await page.click('#aiOrb');
    await page.waitForSelector('#chat.is-active .chat-text-input');
    const hasCherub = await page.locator('#chat .muse-chat-header img[src*="muse_cupid"]').count();
    if (hasCherub > 0) throw new Error('A1 violation: cherub image found in active chat header');
    await page.fill('#chat .chat-text-input', '妈妈膝盖不舒服，我能做点什么？');
    await page.click('#chatOrbBtn');
    await page.waitForSelector('#chat .chat-bubble.ai .ai-para');
    await page.click('#chat .muse-glass-circle-btn');
    await page.waitForSelector('#chat', { state: 'hidden' });
  });

  await step('A2 3-group settings: switch atmosphere to 素雅细格 & reload persistence', async () => {
    await page.click('#settingsBtn');
    await page.waitForSelector('.edition-chip-card');
    const cards = await page.locator('.edition-chip-card').count();
    if (cards !== 3) throw new Error(`A2 violation: expected 3 atmospheres, got ${cards}`);
    await page.click('.edition-chip-card:nth-child(2)');
    await page.waitForFunction(() => document.querySelector('#app')?.dataset.journalEdition === 'hobonichi');
    await page.keyboard.press('Escape');
    await page.reload();
    await page.waitForSelector('.mat-card');
    if ((await page.locator('#app').getAttribute('data-journal-edition')) !== 'hobonichi') {
      throw new Error('hobonichi atmosphere did not persist across reload');
    }
  });
} catch {
  /* reported in summary */
} finally {
  await browser.close();
  console.log(steps.join('\n'));
  if (problems.length) console.log('\nProblems:\n' + [...new Set(problems)].join('\n'));
  const failed = steps.some((s) => s.startsWith('FAIL')) || problems.length > 0;
  process.exit(failed ? 1 : 0);
}
