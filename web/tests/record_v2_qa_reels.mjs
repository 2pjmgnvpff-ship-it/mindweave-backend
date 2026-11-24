#!/usr/bin/env node
/**
 * MindWeave (心络) V2 — 5-Reel Full-Motion .mp4 QA Recorder & CDP Telemetry Harness (03d G12)
 *
 * Strictly enforces Global Rule #3: continuous uncut H.264 .mp4 recordings (< 15 MB each, 30fps)
 * for native `view_file` visual & motion audit — NEVER keyframe-only or contact-sheet sign-off.
 *
 * Usage:
 *   node scratch/record_v2_qa_reels.mjs --base-url http://127.0.0.1:8080 --viewport 390x844
 *   node scratch/record_v2_qa_reels.mjs --base-url http://127.0.0.1:8080 --viewport both --reels V1,V2,V3,V4,V5
 *   node scratch/record_v2_qa_reels.mjs --smoke-styleguide   # Dry-run verifies Chromium+CDP+ffmpeg pipeline live
 */

import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { chromium } = require('/home/antigravity/e2e/node_modules/playwright');

const SCRATCH_DIR = path.resolve(
  '/home/antigravity/.gemini/jetski/brain/v1/default/jetski_remote_agents_3p/eaa2f91f-bad0-438a-9162-823e76874a68/scratch'
);
const STYLEGUIDE_URL = pathToFileURL(path.join(SCRATCH_DIR, 'gleb_v2_living_styleguide.html')).href;
const CHROMIUM_PATH = '/usr/bin/chromium';
const MAX_SINGLE_MP4_BYTES = 15 * 1024 * 1024; // 15 MB target (< 100 MB view_file hard cap)

const VIEWPORTS = {
  '390x844': { width: 390, height: 844, dpr: 3, tag: 'iphone15_390x844' },
  '375x667': { width: 375, height: 667, dpr: 2, tag: 'iphonese_375x667' },
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function parseArgs(argv) {
  const opts = {
    baseUrl: 'http://127.0.0.1:8765',
    outDir: path.join(SCRATCH_DIR, 'qa_reels'),
    reels: ['V1', 'V2', 'V3', 'V4', 'V5'],
    viewport: '390x844',
    cpuRate: 4,
    email: `reels-qa-${Date.now()}@mindweave.test`,
    smokeStyleguide: false,
  };
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === '--base-url') opts.baseUrl = argv[++i];
    else if (a === '--out-dir') opts.outDir = path.resolve(argv[++i]);
    else if (a === '--reels') opts.reels = argv[++i].split(',').map((s) => s.trim().toUpperCase()).filter(Boolean);
    else if (a === '--viewport') opts.viewport = argv[++i];
    else if (a === '--cpu-rate') opts.cpuRate = Number(argv[++i]) || 4;
    else if (a === '--email') opts.email = argv[++i];
    else if (a === '--smoke-styleguide') opts.smokeStyleguide = true;
  }
  return opts;
}

function percentile(arr, p) {
  if (!arr.length) return 0;
  const s = [...arr].sort((a, b) => a - b);
  return Number(s[Math.min(s.length - 1, Math.max(0, Math.ceil((p / 100) * s.length) - 1))].toFixed(2));
}

function summarizeTelemetry(raw) {
  const f = raw.frameDeltas || [];
  const p50 = percentile(f, 50);
  const loafs = raw.loafs || [];
  return {
    frameCount: f.length,
    p50Ms: p50,
    p95Ms: percentile(f, 95),
    p99Ms: percentile(f, 99),
    maxFrameMs: f.length ? Number(Math.max(...f).toFixed(2)) : 0,
    medianFps: p50 > 0 ? Number(Math.min(60, 1000 / p50).toFixed(1)) : 60,
    droppedOver24ms: f.filter((x) => x > 24).length,
    loafsOver50ms: loafs.filter((x) => x > 50).length,
    maxLoafMs: loafs.length ? Number(Math.max(...loafs).toFixed(1)) : 0,
    maxMountedMatCards: raw.maxMountedMatCards || 0,
    maxOverflowX: Number((raw.maxOverflowX || 0).toFixed(1)),
    maxViewportScale: Number((raw.maxViewportScale || 1).toFixed(3)),
    consoleErrors: raw.consoleErrors || [],
    pageErrors: raw.pageErrors || [],
  };
}

/** Real CDP touch drag/fling helper (1-finger) */
async function touchSwipe(cdp, x0, y0, x1, y1, { steps = 14, stepMs = 14, holdBeforeEndMs = 0 } = {}) {
  await cdp.send('Input.dispatchTouchEvent', {
    type: 'touchStart',
    touchPoints: [{ x: Math.round(x0), y: Math.round(y0), id: 1 }],
  });
  for (let i = 1; i <= steps; i += 1) {
    const t = i / steps;
    await cdp.send('Input.dispatchTouchEvent', {
      type: 'touchMove',
      touchPoints: [{ x: Math.round(x0 + (x1 - x0) * t), y: Math.round(y0 + (y1 - y0) * t), id: 1 }],
    });
    await sleep(stepMs);
  }
  if (holdBeforeEndMs > 0) await sleep(holdBeforeEndMs);
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
}

/** Real CDP 2-finger pinch helper (tests 04d dynamic window TouchEvent pinch inside zoom-locked viewport) */
async function touchPinch(cdp, cx, cy, startHalfDist, endHalfDist, { steps = 12, stepMs = 16 } = {}) {
  const pts = (d) => [
    { x: Math.round(cx - d), y: Math.round(cy - d * 0.4), id: 1 },
    { x: Math.round(cx + d), y: Math.round(cy + d * 0.4), id: 2 },
  ];
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: pts(startHalfDist) });
  for (let i = 1; i <= steps; i += 1) {
    const d = startHalfDist + ((endHalfDist - startHalfDist) * i) / steps;
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: pts(d) });
    await sleep(stepMs);
  }
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
}

async function safeClick(page, selectors, waitAfterMs = 450) {
  for (const sel of selectors) {
    const loc = page.locator(sel).first();
    if ((await loc.count()) > 0 && (await loc.isVisible().catch(() => false))) {
      await loc.click({ timeout: 2500 }).catch(() => {});
      await sleep(waitAfterMs);
      return true;
    }
  }
  return false;
}

/** Inject rAF frame-delta + W3C LoAF + DOM/overflow/scale collector before app scripts run */
function telemetryInitScript() {
  window.__mwQaTelemetry = {
    frameDeltas: [],
    loafs: [],
    maxMountedMatCards: 0,
    maxOverflowX: 0,
    maxViewportScale: 1,
  };
  let lastTs = 0;
  let tick = 0;
  function rafLoop(ts) {
    if (lastTs > 0) {
      const dt = ts - lastTs;
      if (dt > 0 && dt < 1000) window.__mwQaTelemetry.frameDeltas.push(dt);
    }
    lastTs = ts;
    tick += 1;
    if (tick % 10 === 0 && document.documentElement) {
      const cards = document.querySelectorAll('.mat-card').length;
      if (cards > window.__mwQaTelemetry.maxMountedMatCards) window.__mwQaTelemetry.maxMountedMatCards = cards;
      const ov = document.documentElement.scrollWidth - window.innerWidth;
      if (ov > window.__mwQaTelemetry.maxOverflowX) window.__mwQaTelemetry.maxOverflowX = ov;
      const sc = window.visualViewport ? window.visualViewport.scale : 1;
      if (sc > window.__mwQaTelemetry.maxViewportScale) window.__mwQaTelemetry.maxViewportScale = sc;
    }
    requestAnimationFrame(rafLoop);
  }
  requestAnimationFrame(rafLoop);
  try {
    const po = new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) {
        if (entry.duration > 30) window.__mwQaTelemetry.loafs.push(entry.duration);
      }
    });
    po.observe({ type: 'long-animation-frame', buffered: true });
  } catch {
    // Fallback on browsers without LoAF observer
  }
}

/** Convert Playwright WebM -> H.264 yuv420p 30fps +faststart MP4 (< 15 MB) */
function transcodeWebmToMp4(webmPath, mp4Path) {
  execFileSync(
    'ffmpeg',
    [
      '-y', '-loglevel', 'error',
      '-i', webmPath,
      '-c:v', 'libx264',
      '-preset', 'fast',
      '-crf', '22',
      '-pix_fmt', 'yuv420p',
      '-r', '30',
      '-movflags', '+faststart',
      mp4Path,
    ],
    { stdio: 'pipe' }
  );
  const stat = fs.statSync(mp4Path);
  const durationStr = execFileSync(
    'ffprobe',
    ['-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', mp4Path],
    { encoding: 'utf8' }
  ).trim();
  const durationSec = Number(Number(durationStr || 0).toFixed(2));
  const segments = [];
  // Global Rule #3: if a single recording exceeds 90 MB or 65s, auto-segment cleanly into 50s lossless slices
  if (stat.size > 90 * 1024 * 1024 || durationSec > 65) {
    const segPattern = mp4Path.replace(/\.mp4$/, '_seg%02d.mp4');
    execFileSync(
      'ffmpeg',
      ['-y', '-loglevel', 'error', '-i', mp4Path, '-c', 'copy', '-f', 'segment', '-segment_time', '50', '-reset_timestamps', '1', segPattern],
      { stdio: 'pipe' }
    );
    const dir = path.dirname(mp4Path);
    const prefix = path.basename(mp4Path).replace(/\.mp4$/, '_seg');
    for (const f of fs.readdirSync(dir).sort()) {
      if (f.startsWith(prefix) && f.endsWith('.mp4')) segments.push(path.join(dir, f));
    }
  }
  fs.rmSync(webmPath, { force: true });
  return { bytes: stat.size, mb: Number((stat.size / (1024 * 1024)).toFixed(2)), durationSec, under15MB: stat.size <= MAX_SINGLE_MP4_BYTES, segments };
}

/** Off-camera real API seeder (runs between V1 and V2 using V1's live JWT so V2-V5 show rich multi-month/2-year/Silver data) */
async function seedRichJournalData(baseUrl, authStatePath) {
  if (!fs.existsSync(authStatePath)) return;
  const state = JSON.parse(fs.readFileSync(authStatePath, 'utf8'));
  const origin = (state.origins || []).find((o) => (o.localStorage || []).some((kv) => kv.name === 'mw.auth'));
  const rawAuth = origin?.localStorage?.find((kv) => kv.name === 'mw.auth')?.value;
  if (!rawAuth) return;
  const auth = JSON.parse(rawAuth);
  const token = auth.accessToken;
  if (!token) return;
  const apiBase = `${baseUrl.replace(/\/$/, '')}/v1`;
  let createdMaterials = 0;
  const req = async (method, p, body) => {
    const res = await fetch(`${apiBase}${p}`, {
      method,
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
        ...(method === 'POST' ? { 'Idempotency-Key': crypto.randomUUID() } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) {
      const txt = await res.text().catch(() => '');
      console.warn(`[QA-REEL] Seed ${method} ${p} -> ${res.status} ${txt.slice(0, 120)}`);
      return null;
    }
    return res.json().catch(() => ({}));
  };
  const boot = await req('GET', '/bootstrap');
  const existing = Object.values(boot?.people || {});
  let mom = existing.find((p) => p.nameCn === '妈妈');
  if (!mom) mom = await req('POST', '/persons', { id: crypto.randomUUID(), nameCn: '妈妈', tier: 'gold', roleTag: '家人' });
  const lin = existing.find((p) => p.nameCn === '林晚') || await req('POST', '/persons', { id: crypto.randomUUID(), nameCn: '林晚', tier: 'gold', roleTag: '挚友' });
  const chen = existing.find((p) => p.nameCn === '陈经理') || await req('POST', '/persons', { id: crypto.randomUUID(), nameCn: '陈经理', tier: 'silver', roleTag: '工作' });
  const yu = existing.find((p) => p.nameCn === '小宇') || await req('POST', '/persons', { id: crypto.randomUUID(), nameCn: '小宇', tier: 'crystal', roleTag: '邻居' });

  const momEntries = [
    { title: '入冬护膝保暖', body: '妈妈说入冬以后早晨出门风大，膝盖发凉，冬天保暖要给她挑一副透气的羊绒护膝。', happenedOn: '2026-11-18', pinned: true },
    { title: '社区医院血压复查', body: '陪妈妈去社区医院测了血压，医生说控制得很稳，少吃腌菜多散步。', happenedOn: '2026-11-06', pinned: false },
    { title: '立冬包白菜猪肉饺子', body: '立冬那天妈妈提前发好面，特意给我留了一盘少盐的三鲜馅，叮嘱天冷多穿棉袜。', happenedOn: '2026-11-02', pinned: false },
    { title: '重阳植物园赏桂', body: '重阳节带妈妈去了植物园看桂花，她一直夸金桂香得让人安心，还拍了好多照。', happenedOn: '2026-10-19', pinned: false },
    { title: '寒露泡老白茶', body: '妈妈说最近睡前喝一点温热的寿眉老白茶很舒服，不会影响睡眠。', happenedOn: '2026-10-08', pinned: false },
    { title: '中秋低糖月饼', body: '中秋给妈妈寄了低糖五仁月饼，她说配热普洱刚刚好，一点也不腻。', happenedOn: '2026-09-24', pinned: false },
    { title: '秋分换软底健步鞋', body: '给妈妈换了一双轻便防滑的健步鞋，37码刚好不磨脚，散步四十分钟也不累。', happenedOn: '2026-09-12', pinned: false },
    { title: '初夏修剪阳台茉莉', body: '妈妈把阳台上的茉莉修剪了一遍，说等明年开春枝条会更旺。', happenedOn: '2026-06-12', pinned: false },
    { title: '谷雨采明前龙井', body: '妈妈喜欢淡淡的绿茶清香，平时泡茶水温别太烫，八十五度刚好。', happenedOn: '2026-04-20', pinned: false },
    { title: '去年今日恒温泡脚桶', body: '去年冬天给妈妈买了恒温泡脚桶，她说每晚泡十五分钟睡得特别沉，冬天保暖最实用。', happenedOn: '2025-11-23', pinned: true },
    { title: '去年霜降莲藕排骨汤', body: '去年霜降妈妈炖了莲藕排骨汤，特意叮嘱我降温记得加毛衣。', happenedOn: '2025-10-25', pinned: false },
    { title: '去年母亲节康乃馨', body: '去年母亲节送了浅杏色康乃馨，妈妈插在白瓷瓶里放了整整两周。', happenedOn: '2025-05-11', pinned: false },
  ];
  if (mom?.id) {
    for (const m of momEntries) {
      if (await req('POST', `/persons/${mom.id}/materials`, { kind: 'note', ...m })) createdMaterials += 1;
    }
    await req('POST', '/captures?sync=true', { id: crypto.randomUUID(), source: 'text', text: '妈妈下周三上午九点要去三院复查，记得提前帮她带好医保卡和旧片子', routeMode: 'current', activePersonId: mom.id });
  }
  if (lin?.id) {
    for (const m of [
      { title: '饮品偏好与京都计划', body: '林晚不吃香菜，咖啡固定点燕麦拿铁去冰，下个月去京都想一起逛哲学之道。', happenedOn: '2026-11-12', pinned: true },
      { title: '旧诗集扉页', body: '林晚送了一本装帧极好的旧诗集，扉页写着慢一点也没关系。', happenedOn: '2026-08-15', pinned: false },
    ]) {
      if (await req('POST', `/persons/${lin.id}/materials`, { kind: 'note', ...m })) createdMaterials += 1;
    }
  }
  if (chen?.id) {
    for (const m of [
      { title: 'Q4方案评审时间确认', body: '陈经理在周会再次确认 Q4 方案评审改到周四下午两点，先书面发纪要再口头同步。', happenedOn: '2026-11-16', pinned: true },
      { title: '汇报偏好与沟通边界', body: '陈经理习惯所有预算数字保留到千位，尽量用三点式结论开场，减少情绪表达。', happenedOn: '2026-10-08', pinned: false },
      { title: '合同条款复核节点', body: '涉及外部合作方报价变更时，一律留邮件书面记录，不做口头承诺。', happenedOn: '2026-09-14', pinned: false },
    ]) {
      if (await req('POST', `/persons/${chen.id}/materials`, { kind: 'note', ...m })) createdMaterials += 1;
    }
    await req('POST', '/captures?sync=true', { id: crypto.randomUUID(), source: 'text', text: '陈经理交代周五前把合作方的合同条款走完法务复核', routeMode: 'current', activePersonId: chen.id });
  }
  if (yu?.id) {
    if (await req('POST', `/persons/${yu.id}/materials`, { kind: 'note', title: '楼下橘猫汤圆', body: '小宇养了一只叫汤圆的橘猫，周末经常在楼下花园晒太阳。', happenedOn: '2026-11-10', pinned: false })) createdMaterials += 1;
  }
  console.log(`[QA-REEL] Seeded ${createdMaterials} multi-month/2-year/Silver materials across 4 people onto live backend.`);
}

/** 5 Uncut Real-User Journey Scripts (03d G12 — 1:1 aligned to live V2 DOM IDs) */
const REEL_SPECS = {
  V1: {
    name: 'V1_auth_onboarding_zero_entry',
    title: 'First 60s: Cold Boot -> Email OTP (Single Input) -> 3-Step Onboarding -> Journal Home',
    async run(page, cdp, vp, ctx) {
      await page.goto(ctx.baseUrl, { waitUntil: 'domcontentloaded' });
      await sleep(900); // Verify Frame-0 #FCFBF9 & zero 60ms keyboard slam
      if (await safeClick(page, ['#authEmail', 'input[type="email"]'], 300)) {
        await page.keyboard.type(ctx.email, { delay: 25 });
        await sleep(250);
        await safeClick(page, ['#authSendCode', '.auth-primary', 'button:has-text("获取验证码")'], 950);
      }
      const codeEl = page.locator('#authCode').first();
      await codeEl.waitFor({ state: 'visible', timeout: 5000 }).catch(() => {});
      if ((await codeEl.count()) > 0) {
        const val = await codeEl.inputValue().catch(() => '');
        if (val.length < 6) {
          await codeEl.click().catch(() => {});
          await page.keyboard.type('123456', { delay: 40 });
        }
        await sleep(350);
        await safeClick(page, ['#authVerify', 'button:has-text("翻开手帐")'], 1100);
      }
      // Step 3: Onboarding Sanctuary (136px feathered Cupid medallion -> first slip -> Gold tier -> enter)
      await page.locator('.onb-example-chip').first().waitFor({ state: 'visible', timeout: 6000 }).catch(() => {});
      await sleep(500);
      await safeClick(page, ['.onb-example-chip', 'button:has-text("妈妈最近总说膝盖疼")'], 450);
      await safeClick(page, ['#onbSend', '.onb-send'], 1600);
      // Handle askName fallback if backend didn't auto-split a person name
      const nameInput = page.locator('#onbName').first();
      if ((await nameInput.count()) > 0 && (await nameInput.isVisible().catch(() => false))) {
        await nameInput.click().catch(() => {});
        await page.keyboard.type('妈妈', { delay: 45 });
        await sleep(250);
        await safeClick(page, ['button:has-text("确认称呼")'], 1100);
      }
      await page.locator('.onb-arrow-card[data-tier="gold"]').first().waitFor({ state: 'visible', timeout: 7000 }).catch(() => {});
      await sleep(450);
      await safeClick(page, ['.onb-arrow-card[data-tier="gold"]', 'button:has-text("金箭")'], 1400);
      await page.locator('#onbEnter').first().waitFor({ state: 'visible', timeout: 7000 }).catch(() => {});
      await sleep(700);
      await safeClick(page, ['#onbEnter', 'button:has-text("翻开")'], 1100);
      await sleep(750);
      if (ctx.authStatePath) {
        await page.context().storageState({ path: ctx.authStatePath }).catch(() => {});
      }
    },
  },

  V2: {
    name: 'V2_3scale_journal_spine_search',
    title: 'Day-900 Scale: River Fling -> 3-Scale FLIP & 2-Finger Pinch -> Spine -> Semantic Search -> Inner-4 & All-People',
    async run(page, cdp, vp, ctx) {
      await page.goto(ctx.baseUrl, { waitUntil: 'networkidle' }).catch(() => {});
      await sleep(850);
      // 1-finger continuous vertical flings across Scale-1 Virtual River (12+ slips)
      for (let i = 0; i < 3; i += 1) {
        await touchSwipe(cdp, vp.width * 0.5, vp.height * 0.72, vp.width * 0.5, vp.height * 0.22, { steps: 12, stepMs: 14 });
        await sleep(420);
      }
      await touchSwipe(cdp, vp.width * 0.5, vp.height * 0.26, vp.width * 0.5, vp.height * 0.78, { steps: 12, stepMs: 14 });
      await sleep(500);
      // Scale-1 -> Scale-2 Monthly Gallery (月册)
      await safeClick(page, ['.scale-pill-btn:has-text("月册")', '#folioMonthBtn'], 900);
      await touchSwipe(cdp, vp.width * 0.5, vp.height * 0.68, vp.width * 0.5, vp.height * 0.30, { steps: 10, stepMs: 14 });
      await sleep(500);
      // Scale-2 -> Scale-3 Yearly Rings (年轮)
      await safeClick(page, ['.scale-pill-btn:has-text("年轮")', 'button:has-text("年轮")'], 900);
      await sleep(600);
      // 2-finger pinch back into Scale-2 -> tap month into Scale-1
      await touchPinch(cdp, vp.width * 0.5, vp.height * 0.5, 42, 118);
      await sleep(550);
      await safeClick(page, ['.scale2-card', '.scale-pill-btn:has-text("纸页")'], 750);
      // Semantic Search ("冬天保暖" -> hits real seeded entries!)
      if (await safeClick(page, ['#searchToggleBtn', 'button[aria-label*="搜索"]'], 350)) {
        await safeClick(page, ['#spotlightInput'], 150);
        await page.keyboard.type('冬天保暖', { delay: 48 });
        await sleep(950);
        await safeClick(page, ['#spotlightClose', 'button[aria-label="关闭搜索"]'], 450);
      }
      // B1 Inner-4 Porcelain Tray (#inner4Tray) -> All-People Sheet
      await safeClick(page, ['.capsule-person-btn', '#capsule'], 650);
      await safeClick(page, ['.inner4-chip.is-all', 'button:has-text("全部人物")'], 750);
      await touchSwipe(cdp, vp.width * 0.5, vp.height * 0.7, vp.width * 0.5, vp.height * 0.32, { steps: 10, stepMs: 14 });
      await sleep(500);
      await safeClick(page, ['button[aria-label="关闭"]', '.mw-sheet-close'], 500);
    },
  },

  V3: {
    name: 'V3_voice_receipt_archiving_crud',
    title: 'Subway Capture: #capsuleVoiceBtn Hold/Cancel -> #captureReceipt -> C1「最近归档记录」CRUD',
    async run(page, cdp, vp, ctx) {
      await page.goto(ctx.baseUrl, { waitUntil: 'networkidle' }).catch(() => {});
      await sleep(700);
      const voiceBtn = page.locator('#capsuleVoiceBtn, .capsule-voice-btn').first();
      if ((await voiceBtn.count()) > 0 && (await voiceBtn.isVisible().catch(() => false))) {
        const box = await voiceBtn.boundingBox();
        if (box) {
          const cx = box.x + box.width / 2;
          const cy = box.y + box.height / 2;
          // Gesture 1: Hold -> Slide UP -96px -> Cancel
          await touchSwipe(cdp, cx, cy, cx, cy - 96, { steps: 10, stepMs: 35, holdBeforeEndMs: 220 });
          await sleep(650);
          // Gesture 2: Hold 650ms -> Release cleanly -> #captureReceipt Routing Pill
          await touchSwipe(cdp, cx, cy, cx + 2, cy - 4, { steps: 6, stepMs: 90, holdBeforeEndMs: 180 });
          await sleep(1100);
        }
      }
      await safeClick(page, ['#captureReceipt', '.capture-receipt-pill', 'button:has-text("展开")'], 600);
      await safeClick(page, ['button:has-text("全部最近归档")'], 650);
      if ((await page.locator('.archive-run-card').count()) === 0) {
        await safeClick(page, ['.capsule-person-btn', '#capsule'], 550);
        await safeClick(page, ['.inner4-chip.is-archive', 'button:has-text("最近归档")'], 850);
      }
      await safeClick(page, ['summary:has-text("原话")'], 500);
      await safeClick(page, ['.archive-filter-chip:has-text("待确认")', 'button:has-text("待确认")'], 450);
      await safeClick(page, ['.archive-filter-chip:has-text("全部")', 'button:has-text("全部")'], 500);
      await safeClick(page, ['.archive-add-btn', 'button:has-text("补拆")'], 600);
      await sleep(650);
    },
  },

  V4: {
    name: 'V4_cupid_chat_drawer_b4_voice',
    title: 'Cupid Chat: A1 24px Header Orb -> B4 Dual Voice & IME -> Borderless Prose -> 1:1 Swipeable Memory Drawer',
    async run(page, cdp, vp, ctx) {
      await page.goto(ctx.baseUrl, { waitUntil: 'networkidle' }).catch(() => {});
      await sleep(700);
      await safeClick(page, ['#aiOrb', '.dock-ai-orb'], 850);
      await sleep(500);
      await safeClick(page, ['.b4-tipkit-popover'], 250);
      if (await safeClick(page, ['#chat .chat-text-input', '#chat textarea'], 300)) {
        await page.keyboard.type('帮我结合手帐看看妈妈下周复查要准备什么', { delay: 28 });
        await sleep(500);
        await safeClick(page, ['#chatOrbBtn', 'button[aria-label*="发送"]'], 1900);
      }
      // Pull open 1:1 finger-tracked Right-Edge Memory Drawer (.chat-material-drawer, 0 parent scale!)
      await touchSwipe(cdp, vp.width - 26, vp.height * 0.5, vp.width * 0.22, vp.height * 0.5, { steps: 14, stepMs: 14 });
      await sleep(650);
      if ((await page.locator('.chat-material-drawer.is-open').count()) === 0) {
        await safeClick(page, ['.muse-drawer-pill'], 600);
      }
      const cards = page.locator('.drawer-mat-card');
      const n = Math.min(2, await cards.count());
      for (let i = 0; i < n; i += 1) {
        await cards.nth(i).click().catch(() => {});
        await sleep(300);
      }
      await safeClick(page, ['.drawer-sticky-cta', 'button:has-text("去聊")'], 650);
      await safeClick(page, ['.cite-chip', '.cite-pill'], 650);
      await sleep(550);
    },
  },

  V5: {
    name: 'V5_silver_settings_faceid_export',
    title: 'Silver Boundary & Sovereignty: 银盾无图 + 隐藏内容 Veil -> A2 3 Atmospheres -> C2 Face ID -> Step-Up Export',
    async run(page, cdp, vp, ctx) {
      await page.goto(ctx.baseUrl, { waitUntil: 'networkidle' }).catch(() => {});
      await sleep(750);
      // Switch to Silver-tier person (陈经理) via #inner4Tray
      await safeClick(page, ['.capsule-person-btn', '#capsule'], 650);
      await safeClick(page, ['.inner4-tile:has-text("陈经理")', 'button:has-text("陈经理")', '[data-tier="silver"]'], 950);
      // Toggle Silver「隐藏内容」opaque #F9FAFC privacy veil on & off
      await safeClick(page, ['.silver-veil-toggle', 'button:has-text("隐藏内容")'], 750);
      await safeClick(page, ['.silver-veil-toggle', 'button:has-text("显示正文")'], 650);
      // Open Settings (A2 3 groups: 纸面氛围 / 数据与隐私 / 账号 + C2 Face ID)
      await safeClick(page, ['#settingsBtn', 'button[aria-label*="设置"]'], 750);
      // Cycle all 3 A2 porcelain atmospheres live (晨光白瓷 -> 素雅细格 -> 柔光珠瓷 -> 晨光白瓷)
      await safeClick(page, ['.edition-chip-card:has-text("素雅细格")'], 650);
      await safeClick(page, ['.edition-chip-card:has-text("柔光珠瓷")'], 650);
      await safeClick(page, ['.edition-chip-card:has-text("晨光白瓷")'], 550);
      // Toggle C2 Face ID switch + grace chip live on camera
      await safeClick(page, ['.mw-switch-btn'], 650);
      await safeClick(page, ['.settings-grace-chip:has-text("离开 1 分钟")'], 450);
      // Open Step-Up Reauth Export sheet & request dev OTP to inspect horizontal 6-slot OTP row
      await safeClick(page, ['button:has-text("导出全部手帐")'], 700);
      await safeClick(page, ['button:has-text("发送安全验证码")'], 950);
      await sleep(750);
    },
  },
};

/** Live self-test against scratch/gleb_v2_living_styleguide.html proving full MP4 + CDP pipeline works */
async function runStyleguideSmoke(page, cdp, vp) {
  await page.goto(STYLEGUIDE_URL, { waitUntil: 'load' });
  await sleep(700);
  await safeClick(page, ['button:has-text("素雅细格")'], 550);
  await safeClick(page, ['button:has-text("银盾")'], 550);
  await safeClick(page, ['button:has-text("柔光珠瓷")'], 550);
  await safeClick(page, ['button:has-text("金箭")'], 550);
  await touchSwipe(cdp, vp.width * 0.5, vp.height * 0.72, vp.width * 0.5, vp.height * 0.25, { steps: 12, stepMs: 14 });
  await sleep(600);
}

/** Native iOS C2 Biometrics preview bridge (injected ONLY in V5 so Face ID toggle + grace chips are visible on camera) */
function v5BiometricsPreviewInitScript() {
  window.__MW_IOS_NATIVE__ = true;
  let state = { available: true, enabled: false, graceSeconds: 0, labelCn: '面容 ID' };
  const reply = (reqId, payload) => {
    setTimeout(() => window.__mwNativeEvent?.({ type: 'biometrics.reply', reqId, ok: true, ...payload }), 30);
  };
  window.webkit = window.webkit || {};
  window.webkit.messageHandlers = window.webkit.messageHandlers || {};
  window.webkit.messageHandlers.mwBiometrics = {
    postMessage(msg) {
      const m = msg || {};
      if (m.action === 'getStatus' || m.action === 'status') return reply(m.reqId, state);
      if (m.action === 'setEnabled') {
        state = { ...state, enabled: Boolean(m.enabled), ...(m.graceSeconds != null ? { graceSeconds: Number(m.graceSeconds) } : {}) };
        return reply(m.reqId, state);
      }
      if (m.action === 'setGrace') {
        state = { ...state, graceSeconds: Number(m.graceSeconds ?? 0) };
        return reply(m.reqId, state);
      }
      reply(m.reqId, state);
    },
  };
}

async function recordSingleReel(browser, reelId, vp, opts, { recordVideo = true } = {}) {
  const rawDir = path.join(opts.outDir, 'raw');
  fs.mkdirSync(rawDir, { recursive: true });
  const spec = opts.smokeStyleguide
    ? { name: 'V0_smoke_living_styleguide', title: 'Pipeline Smoke: Living Styleguide 3x3 Porcelain', run: runStyleguideSmoke }
    : REEL_SPECS[reelId];

  const authStatePath = path.join(opts.outDir, 'auth_state.json');
  const useSavedAuth = !opts.smokeStyleguide && reelId !== 'V1' && fs.existsSync(authStatePath);
  const context = await browser.newContext({
    viewport: { width: vp.width, height: vp.height },
    deviceScaleFactor: vp.dpr,
    isMobile: true,
    hasTouch: true,
    locale: 'zh-CN',
    permissions: ['microphone', 'clipboard-read', 'clipboard-write'],
    ...(useSavedAuth ? { storageState: authStatePath } : {}),
    ...(recordVideo ? { recordVideo: { dir: rawDir, size: { width: vp.width, height: vp.height } } } : {}),
  });

  const consoleErrors = [];
  const pageErrors = [];
  let videoHandle = null;
  let telemetry = {};

  try {
    await context.addInitScript(telemetryInitScript);
    if (reelId === 'V5') await context.addInitScript(v5BiometricsPreviewInitScript);
    const page = await context.newPage();
    videoHandle = recordVideo ? page.video() : null;
    page.on('console', (msg) => {
      if (msg.type() === 'error') consoleErrors.push(msg.text());
    });
    page.on('pageerror', (err) => pageErrors.push(String(err)));

    const cdp = await context.newCDPSession(page);
    await cdp.send('Performance.enable');
    await cdp.send('Emulation.setCPUThrottlingRate', { rate: opts.cpuRate });

    await spec.run(page, cdp, vp, { ...opts, authStatePath });
    const rawMetrics = await page.evaluate(() => window.__mwQaTelemetry || {}).catch(() => ({}));
    telemetry = summarizeTelemetry({ ...rawMetrics, consoleErrors, pageErrors });
    await page.close();
  } finally {
    await context.close(); // Flushes recorded WebM cleanly before transcode
  }

  if (!recordVideo) return { reelId, telemetry };
  const rawWebm = await videoHandle.path();
  const mp4Path = path.join(opts.outDir, `${spec.name}_${vp.tag}.mp4`);
  const videoMeta = transcodeWebmToMp4(rawWebm, mp4Path);
  const sidecarPath = mp4Path.replace(/\.mp4$/, '.metrics.json');
  const report = {
    reelId: opts.smokeStyleguide ? 'V0_SMOKE' : reelId,
    title: spec.title,
    viewport: `${vp.width}x${vp.height}@${vp.dpr}x`,
    cpuThrottleRate: opts.cpuRate,
    mp4Path,
    video: videoMeta,
    telemetry,
    passHeuristics: {
      under15MB: videoMeta.under15MB,
      zeroConsoleErrors: telemetry.consoleErrors.length === 0 && telemetry.pageErrors.length === 0,
      zeroHorizontalOverflow: telemetry.maxOverflowX <= 1,
      zoomLocked: telemetry.maxViewportScale <= 1.01,
      mountedCardsWithinCap: telemetry.maxMountedMatCards <= 32,
      loafsOver50msZero: telemetry.loafsOver50ms === 0,
    },
  };
  fs.writeFileSync(sidecarPath, JSON.stringify(report, null, 2), 'utf8');
  return report;
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  fs.mkdirSync(opts.outDir, { recursive: true });
  const vpKeys = opts.viewport === 'both' ? ['390x844', '375x667'] : [opts.viewport];
  const reelsToRun = opts.smokeStyleguide ? ['V0'] : opts.reels.filter((r) => REEL_SPECS[r]);
  const results = [];

  const browser = await chromium.launch({
    executablePath: CHROMIUM_PATH,
    headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu-sandbox', '--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'],
  });

  try {
    for (const vpKey of vpKeys) {
      const vp = VIEWPORTS[vpKey] || VIEWPORTS['390x844'];
      let seeded = false;
      for (const reelId of reelsToRun) {
        const rep = await recordSingleReel(browser, reelId, vp, opts);
        results.push(rep);
        if (reelId === 'V1' && !seeded) {
          seeded = true;
          await seedRichJournalData(opts.baseUrl, path.join(opts.outDir, 'auth_state.json')).catch((e) => console.warn('[QA-REEL] Seed warning:', e.message));
        }
        console.log(
          `[QA-REEL] ${path.basename(rep.mp4Path)} | ${rep.video.durationSec}s | ${rep.video.mb} MB | ` +
            `FPS=${rep.telemetry.medianFps} p95=${rep.telemetry.p95Ms}ms LoAF>50=${rep.telemetry.loafsOver50ms} ` +
            `overflowX=${rep.telemetry.maxOverflowX}px errs=${rep.telemetry.consoleErrors.length + rep.telemetry.pageErrors.length}`
        );
      }
    }
  } finally {
    await browser.close();
  }

  const summaryPath = path.join(opts.outDir, 'qa_reels_summary.json');
  fs.writeFileSync(summaryPath, JSON.stringify({ generatedAt: new Date().toISOString(), results }, null, 2), 'utf8');
  console.log(`[QA-REEL] Complete. Summary written to ${summaryPath}`);
}

main().catch((err) => {
  console.error('[QA-REEL] FATAL:', err);
  process.exit(1);
});
