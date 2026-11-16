// voice.js — D3 hold-to-talk on the left orb with the V10 split-arc gesture.
// drag up = 自动分析 (global) · right/default = this person (current) · far = cancel (red, no popup).
// < 300 ms → discarded with a quiet '按住说话' hint. Real waveform from an AnalyserNode.
import { $, svg } from '../dom.js';
import { store } from '../store.js';
import { MIC_SVG } from '../icons.js';
import { computeZone, routeFor, decideRelease, shouldAutoStop, formatTimer, MAX_HOLD_MS } from '../voice-logic.js';
import { createRecorder, MicError } from '../recorder.js';
import { submitVoice } from './capture.js';
import { whisper } from './toast.js';
import { actions } from '../actions.js';

const NEAR_CAP_MS = MAX_HOLD_MS - 10000;

let hold = null; // { pointerId, ox, oy, t0, zone, rec, recReady, raf, timer, finished }
let hintTimer = null;

function els() {
  return {
    app: $('#app'),
    orb: $('#voiceOrb'),
    stage: $('#voiceStage'),
    pill: $('#voicePill'),
    canvas: $('#voiceCanvas'),
    timer: $('#voiceTimer'),
    puck: $('#voicePuck'),
    labelRight: $('#voiceLabelRight'),
    live: $('#voiceLive'),
    hint: $('#holdHint'),
  };
}

function isDirectory() {
  return store.get().view === 'directory' || !store.get().activePersonId;
}

function announce(text) {
  const { live } = els();
  if (live) live.textContent = text;
}

export function showHoldHint() {
  const { hint } = els();
  if (!hint) return;
  hint.classList.add('is-visible');
  clearTimeout(hintTimer);
  hintTimer = setTimeout(() => hint.classList.remove('is-visible'), 3200);
}

function setZone(zone) {
  const { stage, app } = els();
  if (!hold) return;
  hold.zone = zone;
  stage.dataset.voiceZone = zone;
  app.dataset.voiceCancel = String(zone === 'cancel');
}

function drawSilk(now) {
  const { canvas } = els();
  if (!hold || !canvas) return;
  const ctx = canvas.getContext('2d');
  const w = canvas.width;
  const hgt = canvas.height;
  const mid = hgt / 2;
  const t = now * 0.0046;
  const level = hold.recReady ? hold.rec.level() : 0.12;
  hold.level = hold.level == null ? level : hold.level * 0.7 + level * 0.3;
  const gain = 0.35 + hold.level * 1.9;
  const zone = hold.zone;
  const ribbons =
    zone === 'cancel'
      ? [
          { color: 'rgba(255, 245, 244, 0.92)', amp: 18, freq: 0.013, speed: 0.95, lw: 4 },
          { color: 'rgba(255, 198, 192, 0.58)', amp: 12, freq: 0.02, speed: -0.75, lw: 3 },
        ]
      : zone === 'global'
        ? [
            { color: 'rgba(218, 150, 56, 0.88)', amp: 24, freq: 0.015, speed: 1.35, lw: 4.2 },
            { color: 'rgba(92, 168, 236, 0.65)', amp: 18, freq: 0.021, speed: -1.05, lw: 3.4 },
            { color: 'rgba(176, 128, 232, 0.45)', amp: 12, freq: 0.028, speed: 1.65, lw: 2.6 },
          ]
        : [
            { color: 'rgba(218, 142, 58, 0.86)', amp: 24, freq: 0.015, speed: 1.25, lw: 4.2 },
            { color: 'rgba(238, 116, 56, 0.48)', amp: 17, freq: 0.021, speed: -0.95, lw: 3.2 },
            { color: 'rgba(182, 128, 92, 0.34)', amp: 11, freq: 0.0275, speed: 1.6, lw: 2.6 },
          ];
  ctx.clearRect(0, 0, w, hgt);
  ribbons.forEach((rb, i) => {
    ctx.beginPath();
    for (let x = 0; x <= w; x += 4) {
      const env = Math.sin((x / w) * Math.PI);
      const y = mid + Math.sin(x * rb.freq + t * rb.speed + i) * Math.cos(x * 0.007 - t * 0.6) * Math.min(rb.amp * gain, mid - 2) * env;
      if (x === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.strokeStyle = rb.color;
    ctx.lineWidth = rb.lw;
    ctx.lineCap = 'round';
    ctx.stroke();
  });
  const { stage } = els();
  stage.style.setProperty('--voice-level', hold.level.toFixed(3));
  hold.raf = requestAnimationFrame(drawSilk);
}

function tick() {
  if (!hold) return;
  const { timer, pill } = els();
  const elapsed = performance.now() - hold.t0;
  timer.textContent = formatTimer(elapsed);
  pill.classList.toggle('voice-near-cap', elapsed >= NEAR_CAP_MS);
  if (shouldAutoStop(elapsed)) {
    announce('已经两分钟啦，先替你收好这一段');
    finish(hold.zone === 'cancel' ? 'current' : hold.zone);
  }
}

function openStage() {
  const { app, stage, orb, labelRight, timer } = els();
  const person = store.get().people[store.get().activePersonId];
  const directory = isDirectory();
  labelRight.textContent = directory ? '默认此人' : `收进「${person?.nameCn || '此人'}」`;
  stage.dataset.directory = String(directory);
  timer.textContent = '0:00';
  stage.classList.add('is-active');
  stage.setAttribute('aria-hidden', 'false');
  app.dataset.voiceHolding = 'true';
  orb.classList.add('is-holding');
}

function closeStage() {
  const { app, stage, orb, puck, pill } = els();
  stage.classList.remove('is-active');
  stage.setAttribute('aria-hidden', 'true');
  app.dataset.voiceHolding = 'false';
  app.dataset.voiceCancel = 'false';
  orb.classList.remove('is-holding');
  puck.classList.remove('is-dragging');
  pill.classList.remove('voice-near-cap');
}

async function begin(e) {
  if (hold) return;
  const { orb } = els();
  const rect = orb.getBoundingClientRect();
  hold = {
    pointerId: e.pointerId ?? 'key',
    ox: rect.left + rect.width / 2,
    oy: rect.top + rect.height / 2,
    t0: performance.now(),
    zone: isDirectory() ? 'global' : 'current',
    rec: createRecorder(),
    recReady: false,
    finished: false,
    level: null,
  };
  const current = hold;
  setZone(current.zone);
  openStage();
  current.raf = requestAnimationFrame(drawSilk);
  current.timer = setInterval(tick, 200);
  announce('正在听，松开收进手帐，往远处拖可以取消');
  try {
    await current.rec.start();
    current.recReady = true;
    if (current.finished) current.rec.discard();
  } catch (err) {
    if (hold === current) teardown();
    current.rec.discard();
    const kind = err instanceof MicError ? err.kind : 'unsupported';
    if (kind === 'denied') {
      whisper('麦克风没打开，先打字记下吧', 3200);
    } else {
      whisper('这台设备暂时录不了音，先打字记下吧', 3200);
    }
    actions.openTextCapture({ personId: isDirectory() ? null : store.get().activePersonId, directory: isDirectory() });
  }
}

function move(e) {
  if (!hold || (e.pointerId !== undefined && e.pointerId !== hold.pointerId)) return;
  const dx = e.clientX - hold.ox;
  const upY = hold.oy - e.clientY;
  setZone(computeZone(dx, upY, { directory: isDirectory() }));
  const { puck, app } = els();
  const appRect = app.getBoundingClientRect();
  puck.style.left = `${e.clientX - appRect.left}px`;
  puck.style.top = `${e.clientY - appRect.top}px`;
  puck.classList.toggle('is-dragging', Math.hypot(dx, upY) > 12);
}

function teardown() {
  if (!hold) return;
  cancelAnimationFrame(hold.raf);
  clearInterval(hold.timer);
  hold = null;
  closeStage();
}

async function finish(forcedZone) {
  if (!hold || hold.finished) return;
  const current = hold;
  current.finished = true;
  const zone = forcedZone || current.zone;
  const durationMs = performance.now() - current.t0;
  const directory = isDirectory();
  const activePersonId = store.get().activePersonId;
  teardown();
  const decision = decideRelease({ zone, durationMs });
  if (decision !== 'submit' || !current.recReady) {
    current.rec.discard();
    if (decision === 'too-short') showHoldHint();
    else if (decision === 'cancel') announce('已取消，什么也没记');
    else whisper('麦克风刚准备好，再按住说一次吧', 2600);
    return;
  }
  const { blob } = await current.rec.stop();
  if (!blob || blob.size < 200) {
    showHoldHint();
    return;
  }
  const route = routeFor(zone, { activePersonId, directory });
  if (!route) return;
  submitVoice({ blob, route });
}

export function initVoice() {
  const { orb, hint } = els();
  orb.prepend(svg(MIC_SVG, 'voice-orb-icon'));
  orb.addEventListener('contextmenu', (e) => e.preventDefault());
  orb.addEventListener('pointerdown', (e) => {
    if (e.button !== undefined && e.button !== 0) return;
    e.preventDefault();
    try {
      orb.setPointerCapture(e.pointerId);
    } catch {
      /* ignore */
    }
    hint.classList.remove('is-visible');
    begin(e);
  });
  orb.addEventListener('pointermove', move);
  orb.addEventListener('pointerup', (e) => {
    if (hold && hold.pointerId === e.pointerId) finish();
  });
  orb.addEventListener('pointercancel', (e) => {
    if (hold && hold.pointerId === e.pointerId) {
      hold.zone = 'cancel';
      finish('cancel');
    }
  });
  // keyboard: hold Space / Enter to talk
  orb.addEventListener('keydown', (e) => {
    if ((e.key === ' ' || e.key === 'Enter') && !e.repeat && !hold) {
      e.preventDefault();
      begin({});
    }
  });
  orb.addEventListener('keyup', (e) => {
    if ((e.key === ' ' || e.key === 'Enter') && hold && hold.pointerId === 'key') {
      e.preventDefault();
      finish();
    }
  });
  // swallow the synthetic click so a short tap only shows the hint
  orb.addEventListener('click', (e) => e.preventDefault());
  $('#holdHintType').addEventListener('click', () => {
    hint.classList.remove('is-visible');
    actions.openTextCapture({ personId: isDirectory() ? null : store.get().activePersonId, directory: isDirectory() });
  });
}
