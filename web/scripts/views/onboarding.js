// onboarding.js — D7 first-time ritual on an empty account:
// "现在最常想起的那个人是谁？" → one sentence (hold to talk, or type) → TA's first page assembles →
// pick an arrow (金/银/水晶) → Cupid's first reply, citing that very sentence → into the journal.
import { h, clear, svg, $, plainFromMarked } from '../dom.js';
import { api, uuid } from '../api/endpoints.js';
import { MIC_SVG, SEND_SVG, CUPID_IMG, ARROW_PIN_IMG } from '../icons.js';
import { TIER_LABEL, shouldSendOnEnter, formatDateCn, todayIso } from '../format.js';
import { MIN_HOLD_MS, MAX_HOLD_MS, extensionFor, formatTimer } from '../voice-logic.js';
import { createRecorder, recordingSupported } from '../recorder.js';
import { personDecor } from '../decor.js';
import { aiBubble, userBubble } from './chat-render.js';
import { actions } from '../actions.js';

const ARROWS = ['gold', 'silver', 'crystal'];
const EXAMPLE_LINES = ['妈妈最近总说膝盖疼', '好朋友不吃香菜，喜欢无糖乌龙', '同事帮我留了靠窗的位子'];

function stage() {
  return $('#onboarding');
}

/** Run the ritual. Resolves with the new person's id when the user steps into the journal. */
export function runOnboarding() {
  return new Promise((resolve) => {
    const root = stage();
    clear(root);
    delete root.dataset.arrowTier;
    root.hidden = false;
    requestAnimationFrame(() => root.classList.add('is-active'));
    const stepDots = [0, 1, 2].map((i) => h('span', { class: ['onb-step-dot', i === 0 && 'is-current'] }));
    const setStep = (idx) => {
      stepDots.forEach((dot, i) => {
        dot.classList.toggle('is-current', i === idx);
        dot.classList.toggle('is-done', i < idx);
      });
    };
    const kicker = h('p', { class: 'onb-kicker', text: '心络 · 丘比特在听' });
    const questionEl = h('h1', { class: 'onb-question', text: '现在最常想起的那个人是谁？' });
    const subEl = h('p', { class: 'onb-sub', text: '说一句关于 TA 的小事就好，剩下的交给我。' });
    const status = h('p', { class: 'onb-status', role: 'status' });
    const inputWrap = h('div', { class: 'onb-input-wrap' });
    root.append(
      h('div', { class: 'onb-steps', 'aria-hidden': 'true' }, ...stepDots),
      h('div', { class: 'onb-cupid-wrap' }, h('span', { class: 'onb-cupid-halo', 'aria-hidden': 'true' }), h('div', { class: 'onb-cupid' }, h('img', { src: CUPID_IMG, alt: '丘比特' }))),
      kicker,
      questionEl,
      subEl,
      inputWrap,
      status,
    );
    let busy = false;

    const finish = (personId) => {
      root.classList.remove('is-active');
      setTimeout(() => {
        root.hidden = true;
        clear(root);
      }, 380);
      resolve(personId);
    };

    async function submit(payload) {
      if (busy) return;
      busy = true;
      status.textContent = '丘比特在认真听…';
      inputWrap.style.opacity = '0.45';
      try {
        const res =
          payload.kind === 'voice'
            ? await api.ingestVoice({ id: uuid(), blob: payload.blob, filename: `voice.${extensionFor(payload.blob.type)}`, routeMode: 'global' })
            : await api.captureText({ id: uuid(), text: payload.text, routeMode: 'global' });
        if (res.crisisCard) {
          actions.showCrisis(res.crisisCard);
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
        status.textContent = err.quiet ? '' : err.message || '网络有点慢，再试一次吧';
      }
    }

    function buildInputs() {
      clear(inputWrap);
      if (recordingSupported()) {
        const hint = h('span', { class: 'onb-orb-hint', text: '按住说话' });
        const orb = h('button', { type: 'button', class: 'onb-orb', 'aria-label': '按住说话，松开就记下' }, svg(MIC_SVG));
        let rec = null;
        let t0 = 0;
        let timer = null;
        const end = async (cancel) => {
          if (!rec) return;
          const r = rec;
          rec = null;
          clearInterval(timer);
          orb.classList.remove('is-holding');
          const held = performance.now() - t0;
          if (cancel || held < MIN_HOLD_MS) {
            r.discard();
            hint.textContent = held < MIN_HOLD_MS ? '要按住说哦，松开就记下' : '按住说话';
            return;
          }
          hint.textContent = '按住说话';
          try {
            const { blob } = await r.stop();
            if (blob && blob.size > 200) submit({ kind: 'voice', blob });
            else hint.textContent = '好像没听到声音，再说一次？';
          } catch {
            hint.textContent = '没录上，打字也可以';
          }
        };
        orb.addEventListener('contextmenu', (e) => e.preventDefault());
        orb.addEventListener('pointerdown', async (e) => {
          if (busy || rec) return;
          e.preventDefault();
          orb.setPointerCapture?.(e.pointerId);
          rec = createRecorder();
          t0 = performance.now();
          orb.classList.add('is-holding');
          hint.textContent = '0:00 · 松开就记下';
          timer = setInterval(() => {
            const ms = performance.now() - t0;
            hint.textContent = `${formatTimer(ms)} · 松开就记下`;
            if (ms >= MAX_HOLD_MS) end(false);
          }, 250);
          try {
            await rec.start();
          } catch {
            const r = rec;
            rec = null;
            clearInterval(timer);
            r?.discard();
            orb.classList.remove('is-holding');
            hint.textContent = '麦克风没打开，打字也一样';
          }
        });
        orb.addEventListener('pointerup', () => end(false));
        orb.addEventListener('pointercancel', () => end(true));
        inputWrap.append(orb, hint, h('span', { class: 'onb-or', text: '或者' }));
      }
      const text = h('textarea', { class: 'onb-text', id: 'onbText', rows: '1', maxlength: '500', 'aria-label': '关于 TA 的一句话', placeholder: '比如：妈妈最近总说膝盖疼' });
      const send = h('button', { type: 'button', class: 'onb-send', id: 'onbSend', 'aria-label': '记下这句', disabled: true }, svg(SEND_SVG));
      const syncText = () => {
        send.disabled = !text.value.trim();
        text.style.height = 'auto';
        text.style.height = `${Math.min(110, text.scrollHeight)}px`;
      };
      const go = () => text.value.trim() && submit({ kind: 'text', text: text.value.trim() });
      text.addEventListener('input', syncText);
      text.addEventListener('keydown', (e) => {
        if (shouldSendOnEnter(e)) {
          e.preventDefault();
          go();
        }
      });
      send.addEventListener('click', go);
      const examples = h(
        'div',
        { class: 'onb-examples', 'aria-label': '试试这样说' },
        h('span', { class: 'onb-examples-label', text: '点一句试试：' }),
        ...EXAMPLE_LINES.map((line) =>
          h('button', {
            type: 'button',
            class: 'onb-example-chip',
            text: line,
            onClick: () => {
              if (busy) return;
              text.value = line;
              syncText();
              text.focus({ preventScroll: true });
            },
          }),
        ),
      );
      inputWrap.append(h('div', { class: 'onb-text-row' }, text, send), examples);
    }

    function askName({ sentence, captureId, undoable }) {
      const input = h('input', { class: 'onb-name-input', id: 'onbName', maxlength: '20', placeholder: '比如：妈妈', 'aria-label': 'TA 的称呼' });
      const ok = h('button', { type: 'button', class: 'onb-enter', text: '就是 TA' });
      const box = h('div', { class: 'onb-name-fallback' }, h('label', { for: 'onbName', text: '丘比特没听出是谁。你平时怎么称呼 TA？' }), input, ok);
      root.appendChild(box);
      const go = async () => {
        const name = input.value.trim();
        if (!name) return input.focus();
        box.remove();
        if (undoable && captureId) await api.undoCapture(captureId).catch(() => {});
        showPage({ name, personId: null, inboxItemId: null, sentence, needsCreate: true });
      };
      ok.addEventListener('click', go);
      input.addEventListener('keydown', (e) => shouldSendOnEnter(e) && (e.preventDefault(), go()));
      setTimeout(() => input.focus(), 80);
    }

    function showPage(ctx) {
      setStep(1);
      const d = personDecor({ id: ctx.personId || ctx.name, tier: 'gold' });
      const todayCn = formatDateCn(todayIso());
      subEl.remove();
      kicker.textContent = '已经为你翻开新的一页';
      questionEl.textContent = `这是「${ctx.name}」的第一页`;
      root.appendChild(
        h(
          'section',
          { class: 'onb-page', 'aria-label': `${ctx.name}的第一页` },
          h('span', { class: 'mono-seal', style: { background: d.sealBg, color: d.sealColor }, 'aria-hidden': 'true', text: Array.from(ctx.name)[0] || '·' }),
          h('span', { class: 'onb-tape', style: { background: d.tapeColor }, 'aria-hidden': 'true' }),
          h('p', { class: 'onb-page-name', text: ctx.name }),
          h('p', { class: 'onb-page-meta', text: todayCn ? `${todayCn} · 第一页手帐` : '第一页手帐' }),
          h('p', { class: 'onb-slip', text: ctx.sentence }),
          h('p', { class: 'onb-page-foot', 'aria-hidden': 'true', text: '✦ 已轻轻夹进这一页' }),
        ),
      );
      setTimeout(() => showArrows(ctx), 980);
    }

    function showArrows(ctx) {
      const box = h('div', { class: 'onb-arrows', role: 'radiogroup', 'aria-label': '给 TA 一支箭' }, h('p', { class: 'onb-arrows-title', text: `给「${ctx.name}」选一支箭` }));
      for (const tier of ARROWS) {
        box.appendChild(
          h(
            'button',
            { type: 'button', class: 'onb-arrow-card', dataset: { tier }, onClick: (e) => chooseArrow(ctx, tier, box, e.currentTarget) },
            h('img', { src: ARROW_PIN_IMG[tier], alt: '' }),
            h('span', {}, h('strong', { text: `${TIER_LABEL[tier].arrow} · ${TIER_LABEL[tier].short}` }), h('small', { text: TIER_LABEL[tier].meaning })),
          ),
        );
      }
      box.appendChild(h('p', { class: 'onb-sub', text: '以后随时可以在 TA 那页右上角换。' }));
      root.appendChild(box);
      box.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }

    async function chooseArrow(ctx, tier, box, chosenBtn) {
      if (box.dataset.busy) return;
      box.dataset.busy = '1';
      box.querySelectorAll('.onb-arrow-card').forEach((btn) => {
        btn.classList.toggle('is-chosen', btn === chosenBtn);
        btn.classList.toggle('is-dimmed', btn !== chosenBtn);
      });
      status.textContent = '正在把这一页装订好…';
      let personId = ctx.personId;
      try {
        if (ctx.needsCreate) {
          const p = await api.createPerson({ id: uuid(), nameCn: ctx.name, tier });
          personId = p.id;
          const cap = await api.captureText({ id: uuid(), text: ctx.sentence, routeMode: 'current', activePersonId: personId });
          ctx.materialId = cap?.splits?.[0]?.materialId || null;
        } else if (ctx.inboxItemId) {
          const r = await api.resolveInbox(ctx.inboxItemId, { action: 'create_person', tier, displayName: ctx.name });
          personId = r.targetPersonId || personId;
        } else if (personId) {
          await api.setTier(personId, tier);
        }
        await actions.refreshBootstrap();
      } catch (err) {
        delete box.dataset.busy;
        box.querySelectorAll('.onb-arrow-card').forEach((btn) => btn.classList.remove('is-chosen', 'is-dimmed'));
        status.textContent = err.message;
        return;
      }
      status.textContent = '';
      box.remove();
      firstReply(personId, ctx, tier);
    }

    function firstReply(personId, ctx, tier) {
      setStep(2);
      root.dataset.arrowTier = tier || 'gold';
      kicker.textContent = '丘比特已经记在心里';
      const bubble = aiBubble();
      const enter = h(
        'button',
        { type: 'button', class: 'onb-enter', id: 'onbEnter', onClick: () => finish(personId) },
        h('span', { text: `翻开「${ctx.name}」的手帐` }),
        h('span', { class: 'onb-enter-arrow', 'aria-hidden': 'true', text: '→' }),
      );
      // A visible, canned first question (not the sentence itself: chat write-back would file it twice).
      const question = '你记住了什么？';
      const reply = h('div', { class: 'onb-reply' }, userBubble(question), bubble.el, enter);
      root.appendChild(reply);
      reply.scrollIntoView({ behavior: 'smooth', block: 'end' });
      const refs = ctx.materialId ? [ctx.materialId] : [];
      api
        .streamChat(personId, { message: question, referencedMaterialIds: refs, clientMessageId: uuid() }, ({ event, data }) => {
          if (event === 'delta') bubble.append(data?.text || '');
          else if (event === 'citation') bubble.addCitation(data, personId);
          else if (event === 'whisper') bubble.setWhisper(data?.text);
          else if (event === 'crisis') {
            bubble.el.remove();
            actions.showCrisis(data);
          }
        })
        .then(() => {
          if (!bubble.started) bubble.setText(`记住了。以后想到「${ctx.name}」的什么，按住左下角说一句就好。`);
        })
        .catch(() => bubble.setText(`记住了。以后想到「${ctx.name}」的什么，按住左下角说一句就好。`));
      // a citation tap inside onboarding should also leave the ritual
      reply.addEventListener(
        'click',
        (e) => {
          if (e.target.closest?.('.cite-pill')) finish(personId);
        },
        true,
      );
    }

    buildInputs();
  });
}
