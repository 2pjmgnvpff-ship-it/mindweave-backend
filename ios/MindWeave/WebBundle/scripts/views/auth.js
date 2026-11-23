// auth.js — Chinese email one-time-code sign-in on the Gleb lakeside bokeh splash,
// followed by a silky Lake→Capsule morph choreography into the journal.
// Tokens live in localStorage for P0 (see api/client.js TODO for P1 hardening).
import { h, clear, svg, $ } from '../dom.js';
import { api, client } from '../api/endpoints.js';
import { SUNBURST_SVG } from '../icons.js';
import { initNativeBridge, haptic, isNativeIos, openServerConfig, prefersReducedMotion } from '../native-bridge.js';

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const LAST_EMAIL_KEY = 'mw.lastEmail';

/** Choreograph full-screen lakeside photo shrinking seamlessly into the bottom dock capsule. */
function playLakeToCapsuleMorph() {
  const actor = $('#morphActor');
  const vp = $('#app');
  if (!actor || !vp || prefersReducedMotion()) return;
  const vpRect = vp.getBoundingClientRect();
  const cap = $('#capsule');
  const capRect = cap ? cap.getBoundingClientRect() : null;
  const hasCap = capRect && capRect.width > 40 && capRect.height > 20;
  const targetW = hasCap ? capRect.width : 156;
  const targetH = hasCap ? capRect.height : 54;
  const targetLeft = hasCap ? capRect.left - vpRect.left : (vpRect.width - targetW) / 2;
  const targetTop = hasCap ? capRect.top - vpRect.top : vpRect.height - targetH - 18;
  const photo = actor.querySelector('.morph-full-photo');
  const glass = actor.querySelector('.morph-capsule-glass');

  actor.style.transition = 'none';
  actor.style.left = '0px';
  actor.style.top = '0px';
  actor.style.width = `${vpRect.width}px`;
  actor.style.height = `${vpRect.height}px`;
  actor.style.borderRadius = '0px';
  actor.style.opacity = '1';
  if (photo) {
    photo.style.transition = 'none';
    photo.style.backgroundPosition = 'center 30%';
  }
  if (glass) {
    glass.style.transition = 'none';
    glass.style.opacity = '0';
  }
  actor.classList.add('is-morphing');
  void actor.offsetWidth;

  requestAnimationFrame(() => {
    actor.style.transition =
      'left 0.58s cubic-bezier(0.22, 1, 0.36, 1), top 0.58s cubic-bezier(0.22, 1, 0.36, 1), width 0.58s cubic-bezier(0.22, 1, 0.36, 1), height 0.58s cubic-bezier(0.22, 1, 0.36, 1), border-radius 0.58s cubic-bezier(0.22, 1, 0.36, 1), opacity 0.24s ease 0.44s';
    actor.style.left = `${Math.round(targetLeft)}px`;
    actor.style.top = `${Math.round(targetTop)}px`;
    actor.style.width = `${Math.round(targetW)}px`;
    actor.style.height = `${Math.round(targetH)}px`;
    actor.style.borderRadius = '999px';
    actor.style.opacity = '0';
    if (photo) {
      photo.style.transition = 'background-position 0.58s cubic-bezier(0.22, 1, 0.36, 1)';
      photo.style.backgroundPosition = 'center 58%';
    }
    if (glass) {
      glass.style.transition = 'opacity 0.4s ease 0.14s';
      glass.style.opacity = '1';
    }
  });

  setTimeout(() => {
    actor.classList.remove('is-morphing');
    actor.removeAttribute('style');
    if (photo) photo.removeAttribute('style');
    if (glass) glass.removeAttribute('style');
  }, 720);
}

/** Show the sign-in splash. Resolves once the user is signed in. */
export function showAuth() {
  initNativeBridge();
  const root = $('#authScreen');
  return new Promise((resolve) => {
    clear(root);
    root.hidden = false;
    root.classList.remove('is-morphing-out');
    let challengeId = null;
    let email = '';

    const errorEl = h('p', { class: 'auth-error', role: 'alert' });
    const card = h('form', { class: 'auth-card', novalidate: true });

    const setError = (msg) => {
      errorEl.textContent = msg || '';
    };

    function renderEmailStep() {
      clear(card);
      setError('');
      const input = h('input', {
        class: 'auth-input',
        id: 'authEmail',
        type: 'email',
        inputmode: 'email',
        autocomplete: 'email',
        placeholder: '你的邮箱',
        'aria-label': '邮箱',
        value: localStorage.getItem(LAST_EMAIL_KEY) || '',
      });
      const btn = h('button', { class: 'auth-primary', type: 'submit', id: 'authSendCode', text: '获取验证码' });
      card.onsubmit = async (e) => {
        e.preventDefault();
        const value = input.value.trim();
        if (!EMAIL_RE.test(value)) {
          haptic('warning');
          setError('邮箱好像少了点什么，再看一眼？');
          input.focus();
          return;
        }
        haptic('light');
        btn.disabled = true;
        btn.textContent = '正在寄出…';
        try {
          const res = await api.requestOtp(value);
          challengeId = res.challengeId;
          email = value;
          localStorage.setItem(LAST_EMAIL_KEY, value);
          haptic('selection');
          renderCodeStep(res.devOtpCode || '');
        } catch (err) {
          haptic('warning');
          setError(err.message || '验证码没寄出去，稍后再试试');
          btn.disabled = false;
          btn.textContent = '获取验证码';
        }
      };
      card.append(h('label', { class: 'auth-label', for: 'authEmail', text: '用邮箱登录，不用记密码' }), input, btn, errorEl);
      setTimeout(() => input.focus({ preventScroll: true }), 60);
    }

    function renderCodeStep(devCode) {
      clear(card);
      setError('');
      const input = h('input', {
        class: 'auth-input is-code',
        id: 'authCode',
        type: 'text',
        inputmode: 'numeric',
        autocomplete: 'one-time-code',
        maxlength: '6',
        placeholder: '······',
        'aria-label': '六位验证码',
        value: devCode,
      });
      const btn = h('button', { class: 'auth-primary', type: 'submit', id: 'authVerify', text: '翻开手帐' });
      const back = h('button', { class: 'auth-link', type: 'button', text: '换个邮箱', onClick: renderEmailStep });
      card.onsubmit = async (e) => {
        e.preventDefault();
        const code = input.value.replace(/\D/g, '');
        if (code.length < 4) {
          haptic('warning');
          setError('验证码是邮件里的六位数字');
          input.focus();
          return;
        }
        btn.disabled = true;
        btn.textContent = '正在翻开…';
        try {
          const res = await api.verifyOtp(challengeId, email, code);
          client.setTokens(res);
          api.grantAiConsent().catch(() => {});
          haptic('success');
          playLakeToCapsuleMorph();
          root.classList.remove('is-active');
          root.hidden = true;
          clear(root);
          resolve({ isNewUser: Boolean(res.isNewUser) });
        } catch (err) {
          haptic('warning');
          setError(err.status === 400 || err.status === 401 || err.status === 422 ? '验证码不对或已过期，再试一次？' : err.message);
          btn.disabled = false;
          btn.textContent = '翻开手帐';
        }
      };
      card.append(
        h('label', { class: 'auth-label', for: 'authCode', text: `验证码已寄到 ${email}` }),
        input,
        btn,
        h('div', { class: 'auth-row' }, h('span', { text: devCode ? '测试环境已自动填好' : '十分钟内有效' }), back),
        errorEl,
      );
      setTimeout(() => input.focus({ preventScroll: true }), 60);
    }

    const serverBtn = isNativeIos()
      ? h('button', {
          type: 'button',
          class: 'auth-server-link',
          text: '服务器连接',
          onClick: () => {
            haptic('light');
            openServerConfig();
          },
        })
      : null;

    root.append(
      h('div', { class: 'splash-photo-sharp', 'aria-hidden': 'true' }),
      h('div', { class: 'splash-bokeh-foreground', 'aria-hidden': 'true' }),
      h('div', { class: 'splash-warm-gradient', 'aria-hidden': 'true' }),
      h('div', { class: 'splash-top-sunburst' }, svg(SUNBURST_SVG, 'splash-sunburst-icon')),
      h(
        'div',
        { class: 'splash-bottom-content' },
        h('h1', { class: 'splash-brand', text: '心络' }),
        h('p', { class: 'splash-quote-text', text: '把在意的人，轻轻记在手帐里' }),
        h('p', { class: 'splash-quote-sub', text: '说一句话，丘比特替你记住' }),
        card,
        h('p', { class: 'auth-fine', text: '你记下的内容只属于你。登录即表示同意由丘比特帮你整理这些文字。' }),
        serverBtn,
      ),
    );
    requestAnimationFrame(() => root.classList.add('is-active'));
    renderEmailStep();
  });
}

export function hideAuth() {
  const root = $('#authScreen');
  root.classList.remove('is-active');
  root.hidden = true;
}
