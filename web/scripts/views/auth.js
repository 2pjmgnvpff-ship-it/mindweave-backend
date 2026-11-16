// auth.js — Chinese email one-time-code sign-in on the lakeside splash.
// Tokens live in localStorage for P0 (see api/client.js TODO for P1 hardening).
import { h, clear, svg, $ } from '../dom.js';
import { api, client } from '../api/endpoints.js';
import { SUNBURST_SVG } from '../icons.js';

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const LAST_EMAIL_KEY = 'mw.lastEmail';

/** Show the sign-in splash. Resolves once the user is signed in. */
export function showAuth() {
  const root = $('#authScreen');
  return new Promise((resolve) => {
    clear(root);
    root.hidden = false;
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
          setError('邮箱好像少了点什么，再看一眼？');
          input.focus();
          return;
        }
        btn.disabled = true;
        btn.textContent = '正在寄出…';
        try {
          const res = await api.requestOtp(value);
          challengeId = res.challengeId;
          email = value;
          localStorage.setItem(LAST_EMAIL_KEY, value);
          renderCodeStep(res.devOtpCode || '');
        } catch (err) {
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
          root.hidden = true;
          clear(root);
          resolve({ isNewUser: Boolean(res.isNewUser) });
        } catch (err) {
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

    root.append(
      h('div', { class: 'splash-photo-sharp', 'aria-hidden': 'true' }),
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
