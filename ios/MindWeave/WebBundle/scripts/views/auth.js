// auth.js — Serene Porcelain Email OTP sign-in (03a / J1-S1..S3 / G04).
// Single <input id="authCode" autocomplete="one-time-code" maxlength="6"> (16px) over 6 optical slots,
// never wipes typed digits on 401, zero 60ms keyboard slam on splash, gated Lake→Capsule morph,
// #if DEBUG (isDebugBuild()) gate on server config, and IDB outbox auto-resume on login.
import { h, clear, $ } from '../dom.js';
import { api, client } from '../api/endpoints.js';
import { friendlyMessage } from '../api/client.js';
import { initNativeBridge, haptic, isDebugBuild, openServerConfig, prefersReducedMotion } from '../native-bridge.js';

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const LAST_EMAIL_KEY = 'mw.lastEmail';

/** Pure GPU-composited exit transition (zero left/top/width/height layout thrash per 05 §10.2). */
function finishAuthExit(root, { isNew }) {
  if (!root) return;
  root.classList.remove('is-active');
  const finish = () => {
    root.hidden = true;
    root.style.display = 'none';
    clear(root);
  };
  if (prefersReducedMotion()) return finish();
  root.classList.add(isNew ? 'is-crossfading' : 'is-morphing-out');
  setTimeout(finish, 260);
}

/** Show the calm lakeside porcelain sign-in splash. Resolves once signed in. */
export function showAuth() {
  initNativeBridge();
  const root = $('#authScreen');
  return new Promise((resolve) => {
    clear(root);
    root.style.display = '';
    root.hidden = false;
    root.classList.remove('is-morphing-out');
    let challengeId = null;
    let email = '';
    let resendTimer = null;

    const errorEl = h('p', { class: 'auth-error', role: 'alert' });
    const card = h('form', { class: 'auth-card', novalidate: true });
    const setError = (msg) => {
      errorEl.textContent = msg || '';
    };

    function renderEmailStep() {
      clearInterval(resendTimer);
      clear(card);
      setError('');
      let savedEmail = '';
      try {
        savedEmail = localStorage.getItem(LAST_EMAIL_KEY) || '';
      } catch {
        /* private mode */
      }
      const input = h('input', {
        class: 'auth-input',
        id: 'authEmail',
        type: 'email',
        inputmode: 'email',
        autocomplete: 'email',
        placeholder: '输入你的邮箱',
        'aria-label': '邮箱',
        value: savedEmail,
      });
      const btn = h('button', { class: 'auth-primary', type: 'submit', id: 'authSendCode', text: '获取验证码' });
      card.onsubmit = async (e) => {
        e.preventDefault();
        const value = input.value.trim();
        if (!EMAIL_RE.test(value)) {
          haptic('warning');
          setError('邮箱格式好像不太对，再检查一下？');
          input.focus();
          return;
        }
        haptic('light');
        btn.disabled = true;
        btn.textContent = '正在寄出…';
        try {
          const reqOtp = api.requestEmailOtp || api.requestOtp;
          const res = await reqOtp(value);
          challengeId = res.challengeId;
          email = value;
          try {
            localStorage.setItem(LAST_EMAIL_KEY, value);
          } catch {
            /* ignore */
          }
          haptic('selection');
          renderCodeStep(res.devOtpCode || '', Number(res.cooldownSeconds || res.retryAfterSec || 60));
        } catch (err) {
          haptic('warning');
          setError(err?.message || friendlyMessage(err?.code, err?.status) || '验证码没寄出去，稍后再试试');
          btn.disabled = false;
          btn.textContent = '获取验证码';
        }
      };
      // J1-S3: Zero 60ms keyboard slam on initial lakeside splash mount!
      card.append(h('label', { class: 'auth-label', for: 'authEmail', text: '用邮箱登录，不用记密码' }), input, btn, errorEl);
      input.addEventListener('focus', () => setTimeout(() => card.scrollIntoView({ behavior: 'smooth', block: 'end' }), 220));
    }

    function renderCodeStep(devCode, cooldownSec = 60) {
      clearInterval(resendTimer);
      clear(card);
      setError('');
      const slots = Array.from({ length: 6 }, () => h('span', { class: 'auth-otp-slot', 'aria-hidden': 'true' }));
      const input = h('input', {
        class: 'auth-otp-input is-code',
        id: 'authCode',
        type: 'text',
        inputmode: 'numeric',
        autocomplete: 'one-time-code',
        maxlength: '6',
        'aria-label': '六位验证码',
        value: String(devCode || '').replace(/\D/g, '').slice(0, 6),
      });
      const syncSlots = () => {
        const digits = input.value.replace(/\D/g, '').slice(0, 6);
        if (input.value !== digits) input.value = digits;
        slots.forEach((slot, i) => {
          slot.textContent = digits[i] || '';
          slot.classList.toggle('is-filled', Boolean(digits[i]));
          slot.classList.toggle('is-active', i === Math.min(digits.length, 5));
        });
      };
      input.addEventListener('input', syncSlots);
      input.addEventListener('focus', () => setTimeout(() => card.scrollIntoView({ behavior: 'smooth', block: 'end' }), 220));
      syncSlots();

      const track = h('div', { class: 'auth-otp-track' }, ...slots, input);
      const btn = h('button', { class: 'auth-primary', type: 'submit', id: 'authVerify', text: '翻开手帐' });
      const back = h('button', { class: 'auth-link', type: 'button', text: '换个邮箱', onClick: renderEmailStep });
      const resendBtn = h('button', { class: 'auth-link', type: 'button', disabled: true });

      let left = cooldownSec;
      const tickResend = () => {
        if (left <= 0) {
          clearInterval(resendTimer);
          resendBtn.disabled = false;
          resendBtn.textContent = '重新寄一封';
        } else {
          resendBtn.disabled = true;
          resendBtn.textContent = `${left} 秒后可重发`;
          left -= 1;
        }
      };
      tickResend();
      resendTimer = setInterval(tickResend, 1000);
      resendBtn.addEventListener('click', async () => {
        resendBtn.disabled = true;
        try {
          const reqOtp = api.requestEmailOtp || api.requestOtp;
          const res = await reqOtp(email);
          challengeId = res.challengeId;
          left = Number(res.cooldownSeconds || res.retryAfterSec || 60);
          tickResend();
          resendTimer = setInterval(tickResend, 1000);
          setError('');
        } catch (err) {
          resendBtn.disabled = false;
          setError(err?.message || friendlyMessage(err?.code, err?.status) || '重发失败，稍后再试');
        }
      });

      card.onsubmit = async (e) => {
        e.preventDefault();
        const code = input.value.replace(/\D/g, '');
        if (code.length < 6) {
          haptic('warning');
          setError('请输入邮件里的六位数字验证码');
          input.focus();
          return;
        }
        btn.disabled = true;
        btn.textContent = '正在翻开…';
        try {
          const res = await api.verifyOtp(challengeId, email, code);
          clearInterval(resendTimer);
          client.setTokens(res);
          api.grantAiConsent().catch(() => {});
          import('./capture.js').then((m) => m.draftQueue?.resumeAuth?.() || m.draftQueue?.resumePaused?.() || m.flushDrafts?.()).catch(() => {});
          haptic('success');
          const isNew = Boolean(res.isNewUser);
          finishAuthExit(root, { isNew });
          resolve({ isNewUser: isNew });
        } catch (err) {
          // J1-S1: preserve all 6 typed digits so user can fix a single typo
          haptic('warning');
          track.classList.add('is-error');
          setTimeout(() => track.classList.remove('is-error'), 420);
          setError(
            err?.status === 400 || err?.status === 401 || err?.status === 422
              ? friendlyMessage(err?.code, err?.status) || '验证码不对或已过期，改一下数字再试'
              : err?.message || '登录未完成，请稍后再试',
          );
          btn.disabled = false;
          btn.textContent = '翻开手帐';
          input.focus({ preventScroll: true });
        }
      };

      card.append(
        h('label', { class: 'auth-label', for: 'authCode', text: `验证码已寄到 ${email}` }),
        track,
        btn,
        h('div', { class: 'auth-row' }, resendBtn, back),
        errorEl,
      );
      input.focus({ preventScroll: true });
    }

    // Release IPA strictly strips raw server-config UI (#if DEBUG only via __MW_DEBUG_BUILD__)
    const serverBtn = isDebugBuild()
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
      h(
        'div',
        { class: 'splash-bottom-content' },
        h('span', { class: 'splash-brand-pill' }, h('span', { class: 'mw-dot', 'aria-hidden': 'true' }), h('span', { text: '私人关系手帐' })),
        h('h1', { class: 'splash-brand', text: '心络' }),
        h('p', { class: 'splash-quote-text', text: '把在意的人，轻轻记在手帐里' }),
        h('p', { class: 'splash-quote-sub', text: '说一句话，丘比特替你记住' }),
        card,
        h('p', { class: 'auth-fine', text: '你记下的内容只属于你，登录即同意由丘比特协助整理文字' }),
        serverBtn,
      ),
    );
    requestAnimationFrame(() => root.classList.add('is-active'));
    renderEmailStep();
  });
}

export function hideAuth() {
  const root = $('#authScreen');
  if (!root) return;
  root.classList.remove('is-active');
  root.hidden = true;
  root.style.display = 'none';
}
