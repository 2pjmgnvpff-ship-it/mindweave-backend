// onboarding.js — 3-Step Sanctuary first-time ritual (03a / J1-S4 / G04 / BC6).
// Delegates 100% of voice hardware lifecycle to voice-fsm.js (zero double-recorder race!),
// 136px feathered muse_cupid_a_listen.webp circular medallion (zero tape/tilt/sparkle),
// level 0° porcelain slip (bottom-right Gregorian date), canonical 金箭·珍重 / 银盾·边界 / 水晶·日常.
import { h, clear, svg, $, plainFromMarked } from '../dom.js';
import { api, uuid } from '../api/endpoints.js';
import { haptic, hapticPrepare, isNativeSpeechAvailable, prefersReducedMotion } from '../native-bridge.js';
import { MIC_SVG, SEND_SVG, CUPID_IMG, ARROW_SVGS } from '../icons.js';
import { TIER_LABEL, formatDateCn, todayIso } from '../format.js';
import { MIN_HOLD_MS, extensionFor, formatTimer } from '../voice-logic.js';
import { recordingSupported } from '../recorder.js';
import { voiceFsm } from '../voice-fsm.js';
import { createImeGuard } from '../lib/ime-guard.js';
import { personDecor } from '../decor.js';
import { aiBubble, userBubble } from './chat-render.js';
import { actions } from '../actions.js';

const ARROWS = ['gold', 'silver', 'crystal'];
const EXAMPLE_LINES = ['妈妈最近总说膝盖疼', '好友不吃香菜，喜欢无糖乌龙', '同事帮我留了靠窗的位置'];

/** Run the 3-step sanctuary onboarding ritual. Resolves with the created person's id. */
export function runOnboarding() {
  return new Promise((resolve) => {
    const root = $('#onboarding');
    clear(root);
    delete root.dataset.arrowTier;
    root.style.display = '';
    root.hidden = false;
    requestAnimationFrame(() => root.classList.add('is-active'));

    const onbAbort = new AbortController();
    const { signal } = onbAbort;
    const stepDots = [0, 1, 2].map((i) => h('span', { class: ['onb-step-dot', i === 0 && 'is-current'] }));
    const setStep = (idx) => stepDots.forEach((dot, i) => (dot.classList.toggle('is-current', i === idx), dot.classList.toggle('is-done', i < idx)));

    const questionEl = h('h1', { class: 'onb-question', text: '把重要的人，轻放在这里' });
    const subEl = h('p', { class: 'onb-sub', text: '说一句关于某个人的小事，丘比特帮你收好' });
    const status = h('p', { class: 'onb-status', role: 'status' });
    const inputWrap = h('div', { class: 'onb-input-wrap' });
    const medallion = h('div', { class: 'onb-cupid-medallion' }, h('img', { src: CUPID_IMG, alt: '丘比特' }));
    root.append(h('div', { class: 'onb-steps', 'aria-hidden': 'true' }, ...stepDots), h('div', { class: 'onb-hero' }, medallion, questionEl, subEl), inputWrap, status);
    let busy = false;
    let activeSid = 0;

    const finish = (personId) => {
      if (activeSid) { voiceFsm.cancel(activeSid); activeSid = 0; }
      onbAbort.abort();
      root.classList.remove('is-active');
      setTimeout(() => { root.hidden = true; root.style.display = 'none'; clear(root); }, 260);
      resolve(personId);
    };

    async function submit(payload) {
      if (busy) return;
      busy = true;
      status.textContent = '丘比特在认真听…';
      inputWrap.style.opacity = '0.45';
      try {
        const res = payload.kind === 'voice'
          ? await api.ingestVoice({ id: uuid(), blob: payload.blob, filename: `voice.${extensionFor(payload.blob.type)}`, routeMode: 'global' })
          : await api.captureText({ id: uuid(), text: payload.text, routeMode: 'global' });
        if (res.safetyCategory === 'crisis' || res.crisisCard) {
          actions.openCrisisCard?.(res.crisisCard || {});
          throw Object.assign(new Error(''), { quiet: true });
        }
        const sentence = res.transcript || plainFromMarked(res.transcriptHtml) || payload.text || '';
        const split = (res.splits || []).find((s) => s.isNewPerson || s.inboxItemId) || (res.splits || [])[0];
        clear(inputWrap);
        inputWrap.style.opacity = '';
        status.textContent = '';
        if (split) showPage({ name: split.personName, personId: split.personId, inboxItemId: split.inboxItemId, materialId: split.materialId, sentence });
        else askName({ sentence, captureId: res.captureId || res.jobId, undoable: Boolean(res.undoDeadline) });
      } catch (err) {
        busy = false;
        inputWrap.style.opacity = '';
        status.textContent = err?.quiet ? '' : err?.message || '网络有点慢，再试一次吧';
      }
    }

    function buildInputs() {
      clear(inputWrap);
      if (isNativeSpeechAvailable() || recordingSupported()) {
        const hint = h('span', { class: 'onb-orb-hint', text: '按住说话' });
        const caption = h('p', { class: 'onb-live-caption', role: 'status', 'aria-live': 'polite', hidden: true });
        const micIcon = svg(MIC_SVG);
        if (micIcon?.style) { micIcon.style.width = '24px'; micIcon.style.height = '24px'; }
        const orb = h('button', { type: 'button', class: 'onb-orb', 'aria-label': '按住说话，松开就记下' }, micIcon);
        let liveText = '';
        let t0 = 0;
        let timer = null;

        const end = async (cancel) => {
          if (!activeSid) return;
          const sid = activeSid;
          activeSid = 0;
          clearInterval(timer);
          orb.classList.remove('is-holding');
          const held = performance.now() - t0;
          if (cancel || held < MIN_HOLD_MS) {
            voiceFsm.cancel(sid);
            caption.hidden = true;
            haptic(cancel ? 'warning' : 'light');
            hint.textContent = held < MIN_HOLD_MS ? '按住说一小句，松开就记下' : '按住说话';
            return;
          }
          hint.textContent = '按住说话';
          haptic('voiceRelease');
          const res = await voiceFsm.stop(sid);
          if (!res) return;
          if (res.native) {
            const spoken = String(res.transcript || liveText || '').trim();
            if (spoken) submit({ kind: 'text', text: spoken });
            else { caption.hidden = true; hint.textContent = '好像没听清，再说一句或直接打字'; }
          } else if (res.blob && res.blob.size > 200) {
            submit({ kind: 'voice', blob: res.blob });
          } else {
            hint.textContent = '好像没听到声音，再说一次？';
          }
        };

        orb.addEventListener('contextmenu', (e) => e.preventDefault(), { signal });
        orb.addEventListener('pointerdown', (e) => {
          if (busy || activeSid) return;
          e.preventDefault();
          hapticPrepare('voiceArm');
          liveText = '';
          caption.textContent = '';
          caption.hidden = true;
          const sid = voiceFsm.acquire('onboarding', {
            mode: 'capture',
            onPartial: (txt) => { liveText = String(txt || ''); caption.hidden = !liveText; caption.textContent = liveText; },
            onPermissionDenied: () => { activeSid = 0; clearInterval(timer); orb.classList.remove('is-holding'); hint.textContent = '请先打开麦克风与语音识别，也可以直接打字'; },
            onError: () => { activeSid = 0; clearInterval(timer); orb.classList.remove('is-holding'); hint.textContent = '麦克风没打开，打字也一样'; },
            onAutoCap: () => end(false),
          });
          if (!sid) return;
          activeSid = sid;
          orb.setPointerCapture?.(e.pointerId);
          haptic('voiceArm');
          t0 = performance.now();
          orb.classList.add('is-holding');
          hint.textContent = '0:00 · 松开就记下';
          timer = setInterval(() => { hint.textContent = `${formatTimer(performance.now() - t0)} · 松开就记下`; }, 250);
        }, { signal });
        orb.addEventListener('pointerup', () => end(false), { signal });
        orb.addEventListener('pointercancel', () => end(true), { signal });
        inputWrap.append(orb, hint, caption, h('span', { class: 'onb-or', text: '或者' }));
      }

      const text = h('textarea', { class: 'onb-text', id: 'onbText', rows: '1', maxlength: '500', 'aria-label': '关于对方的一句话', placeholder: '比如：妈妈最近总说膝盖疼' });
      const sendIcon = svg(SEND_SVG);
      if (sendIcon?.style) { sendIcon.style.width = '18px'; sendIcon.style.height = '18px'; }
      const send = h('button', { type: 'button', class: 'onb-send', id: 'onbSend', 'aria-label': '记下这句', disabled: true }, sendIcon);
      const syncText = () => { send.disabled = !text.value.trim(); text.style.height = 'auto'; text.style.height = `${Math.min(110, text.scrollHeight)}px`; };
      const go = () => text.value.trim() && submit({ kind: 'text', text: text.value.trim() });
      text.addEventListener('input', syncText, { signal });
      createImeGuard(text, { isTouchDevice: Boolean(globalThis.matchMedia?.('(pointer: coarse)')?.matches), onSubmit: go });
      send.addEventListener('click', go, { signal });
      const examples = h('div', { class: 'onb-examples', 'aria-label': '点一句试试' }, h('span', { class: 'onb-examples-label', text: '点一句试试：' }), ...EXAMPLE_LINES.map((line) =>
        h('button', { type: 'button', class: 'onb-example-chip', text: line, onClick: () => { if (!busy) { text.value = line; syncText(); text.focus({ preventScroll: true }); } } }),
      ));
      inputWrap.append(h('div', { class: 'onb-text-row' }, text, send), examples);
    }

    function askName({ sentence, captureId, undoable }) {
      const input = h('input', { class: 'onb-name-input', id: 'onbName', maxlength: '20', placeholder: '比如：妈妈', 'aria-label': '平时怎么称呼' });
      const ok = h('button', { type: 'button', class: 'onb-enter', text: '确认称呼' });
      const box = h('div', { class: 'onb-name-fallback' }, h('label', { for: 'onbName', text: '丘比特没听出是谁，你平时怎么称呼对方？' }), input, ok);
      root.appendChild(box);
      const go = async () => {
        const name = input.value.trim();
        if (!name) return input.focus();
        box.remove();
        if (undoable && captureId) await api.undoCapture(captureId).catch(() => {});
        showPage({ name, personId: null, inboxItemId: null, sentence, needsCreate: true });
      };
      ok.addEventListener('click', go);
      createImeGuard(input, { isTouchDevice: false, onSubmit: go });
      input.focus({ preventScroll: true });
    }

    function showPage(ctx) {
      setStep(1);
      const d = personDecor({ id: ctx.personId || ctx.name, tier: 'gold' });
      const todayCn = formatDateCn(todayIso());
      subEl.remove();
      questionEl.textContent = `这是「${ctx.name}」的第一页`;
      root.appendChild(h('section', { class: 'onb-page', 'aria-label': `${ctx.name}的第一页` },
        h('div', { class: 'onb-page-head' }, h('span', { class: 'mono-seal', style: { background: d.sealBg, color: d.sealColor }, 'aria-hidden': 'true', text: Array.from(ctx.name)[0] || '·' }), h('p', { class: 'onb-page-name', text: ctx.name })),
        h('p', { class: 'onb-slip', text: ctx.sentence }),
        todayCn ? h('p', { class: 'onb-page-date', text: todayCn }) : null,
      ));
      setTimeout(() => showArrows(ctx), 680);
    }

    function showArrows(ctx) {
      const box = h('div', { class: 'onb-arrows', role: 'radiogroup', 'aria-label': '选择关系分类' }, h('p', { class: 'onb-arrows-title', text: `为「${ctx.name}」选一个关系分类` }));
      for (const tier of ARROWS) {
        const meta = TIER_LABEL[tier] || {};
        const emblem = svg(ARROW_SVGS[tier], 'onb-arrow-emblem');
        if (emblem?.style) { emblem.style.width = '20px'; emblem.style.height = '20px'; emblem.style.flexShrink = '0'; }
        box.appendChild(h('button', { type: 'button', role: 'radio', 'aria-checked': 'false', class: 'onb-arrow-card', dataset: { tier }, onClick: (e) => chooseArrow(ctx, tier, box, e.currentTarget) },
          emblem,
          h('span', {}, h('strong', { text: meta.full || `${meta.arrow || ''} · ${meta.short || ''}`.trim() }), h('small', { text: meta.meaning || '' })),
        ));
      }
      box.appendChild(h('p', { class: 'onb-sub', text: '以后随时可以在这一页右上角调整' }));
      root.appendChild(box);
      box.scrollIntoView({ behavior: prefersReducedMotion() ? 'auto' : 'smooth', block: 'center' });
    }

    async function chooseArrow(ctx, tier, box, chosenBtn) {
      if (box.dataset.busy) return;
      haptic('selection');
      box.dataset.busy = '1';
      box.querySelectorAll('.onb-arrow-card').forEach((btn) => { const p = btn === chosenBtn; btn.classList.toggle('is-chosen', p); btn.classList.toggle('is-dimmed', !p); btn.setAttribute('aria-checked', String(p)); });
      status.textContent = '正在收进这一页…';
      let personId = ctx.personId;
      try {
        if (ctx.needsCreate) {
          const p = await api.createPerson({ id: uuid(), nameCn: ctx.name, tier });
          personId = p.id;
          const cap = await api.captureText({ id: uuid(), text: ctx.sentence, routeMode: 'current', activePersonId: personId });
          ctx.materialId = cap?.splits?.[0]?.materialId || null;
        } else if (ctx.inboxItemId) {
          personId = (await api.resolveInbox(ctx.inboxItemId, { action: 'create_person', tier, displayName: ctx.name })).targetPersonId || personId;
        } else if (personId) {
          await api.setTier(personId, tier);
        }
        await actions.refreshBootstrap();
      } catch (err) {
        delete box.dataset.busy;
        box.querySelectorAll('.onb-arrow-card').forEach((btn) => (btn.classList.remove('is-chosen', 'is-dimmed'), btn.setAttribute('aria-checked', 'false')));
        status.textContent = err?.message || '没保存成功，再点一次试试';
        return;
      }
      status.textContent = '';
      box.remove();
      firstReply(personId, ctx, tier);
    }

    function firstReply(personId, ctx, tier) {
      setStep(2);
      root.dataset.arrowTier = tier || 'gold';
      questionEl.textContent = '丘比特已经记下了';
      const bubble = aiBubble();
      const enter = h('button', { type: 'button', class: 'onb-enter', id: 'onbEnter', onClick: () => (haptic('memorySeal', { tier }), finish(personId)) }, h('span', { text: `翻开「${ctx.name}」的手帐` }), h('span', { class: 'onb-enter-arrow', 'aria-hidden': 'true', text: '→' }));
      const question = '你记住了什么？';
      const reply = h('div', { class: 'onb-reply' }, userBubble(question), bubble.el, enter);
      root.appendChild(reply);
      reply.scrollIntoView({ behavior: prefersReducedMotion() ? 'auto' : 'smooth', block: 'end' });
      api.streamChat(personId, { message: question, referencedMaterialIds: ctx.materialId ? [ctx.materialId] : [], clientMessageId: uuid() }, ({ event, data }) => {
        if (event === 'agent_step') bubble.addAgentStep(data);
        else if (event === 'delta') bubble.append(data?.text || '');
        else if (event === 'citation') bubble.addCitation(data, personId, { immediate: true });
        else if (event === 'whisper') bubble.setWhisper(data?.text);
        else if (event === 'crisis') { bubble.el.remove(); actions.openCrisisCard?.(data); }
      }).then(() => {
        bubble.finishTrace();
        bubble.flushCitations();
        if (!bubble.started) bubble.setText(`记住了，以后想到「${ctx.name}」的小事，按住左下角话筒随口说一句就好`);
      }).catch(() => bubble.setText(`记住了，以后想到「${ctx.name}」的小事，按住左下角话筒随口说一句就好`));
      reply.addEventListener('click', (e) => e.target.closest?.('.cite-pill') && finish(personId), true);
    }

    buildInputs();
  });
}
