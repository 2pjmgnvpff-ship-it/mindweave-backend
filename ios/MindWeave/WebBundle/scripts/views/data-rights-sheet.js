// data-rights-sheet.js — Step-Up Reauth, real binary .zip Export + Porcelain SHA-256 Receipt,
// and Apple 5.1.1(v) Account Deletion + Deletion Receipt (01g / 01k / 02g / G11).
import { h, clear } from '../dom.js';
import { api, client } from '../api/endpoints.js';
import { friendlyMessage } from '../api/client.js';
import { store } from '../store.js';
import { haptic, isNativeIos, mwBiometrics, mwNativeUI, shareFileBlob, copyText } from '../native-bridge.js';
import { openSheet, closeSheet } from './sheet.js';
import { whisper } from './toast.js';

const LAST_EMAIL_KEY = 'mw.lastEmail';

function formatBytes(n) {
  const num = Number(n || 0);
  if (!Number.isFinite(num) || num <= 0) return '—';
  if (num < 1024) return `${num} B`;
  if (num < 1024 * 1024) return `${(num / 1024).toFixed(1)} KB`;
  return `${(num / (1024 * 1024)).toFixed(2)} MB`;
}

function todayCnStamp() {
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return { cn: `${y}年${m}月${day}日`, iso: `${y}-${m}-${day}` };
}

function formatCnDateTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso).slice(0, 16);
  const pad = (v) => String(v).padStart(2, '0');
  return `${d.getFullYear()}年${pad(d.getMonth() + 1)}月${pad(d.getDate())}日 ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function currentEmail() {
  const s = store.get();
  if (s?.user?.email) return s.user.email;
  try {
    return localStorage.getItem(LAST_EMAIL_KEY) || '';
  } catch {
    return '';
  }
}

/** Render inline porcelain Step-Up Reauth block; resolves with `reauthToken` on verify. */
function buildReauthPanel({ purposeLabel, onVerified }) {
  const wrap = h('div', { class: 'reauth-panel' });
  const email = currentEmail();
  const errorEl = h('p', { class: 'sheet-inline-error', role: 'alert' });
  let challengeId = null;
  let cooldown = 0;
  let cdTimer = null;

  const emailInput = h('input', {
    class: 'mw-sheet-input',
    type: 'email',
    inputmode: 'email',
    autocomplete: 'email',
    placeholder: '登录邮箱',
    'aria-label': '验证邮箱',
    value: email,
  });

  const codeInput = h('input', {
    class: 'auth-otp-input',
    type: 'text',
    inputmode: 'numeric',
    autocomplete: 'one-time-code',
    maxlength: '6',
    'aria-label': '六位安全验证码',
  });
  const slots = Array.from({ length: 6 }, () => h('span', { class: 'auth-otp-slot', 'aria-hidden': 'true' }));
  const syncSlots = () => {
    const digits = codeInput.value.replace(/\D/g, '').slice(0, 6);
    if (codeInput.value !== digits) codeInput.value = digits;
    slots.forEach((s, i) => {
      s.textContent = digits[i] || '';
      s.classList.toggle('is-filled', Boolean(digits[i]));
      s.classList.toggle('is-active', i === Math.min(digits.length, 5));
    });
  };
  codeInput.addEventListener('input', syncSlots);
  const otpTrack = h('div', { class: 'auth-otp-track', hidden: true, style: { display: 'none' } }, ...slots, codeInput);

  const sendBtn = h('button', { type: 'button', class: 'sheet-secondary-btn', text: '发送安全验证码' });
  const verifyBtn = h('button', { type: 'button', class: 'sheet-primary-btn', text: `验证并${purposeLabel}`, hidden: true, style: { display: 'none' } });

  const startCooldown = (sec = 60) => {
    cooldown = sec;
    sendBtn.disabled = true;
    clearInterval(cdTimer);
    sendBtn.textContent = `${cooldown} 秒后可重发`;
    cdTimer = setInterval(() => {
      cooldown -= 1;
      if (cooldown <= 0 || !sendBtn.isConnected) {
        clearInterval(cdTimer);
        sendBtn.disabled = false;
        sendBtn.textContent = '重新发送验证码';
      } else {
        sendBtn.textContent = `${cooldown} 秒后可重发`;
      }
    }, 1000);
  };

  sendBtn.addEventListener('click', async () => {
    const addr = emailInput.value.trim();
    if (!addr || !addr.includes('@')) {
      haptic('warning');
      errorEl.textContent = '请先填好你的登录邮箱';
      return;
    }
    errorEl.textContent = '';
    sendBtn.disabled = true;
    sendBtn.textContent = '正在寄出…';
    try {
      const reqFn = api.requestReauthOtp || ((e) => client.post('/auth/email/otp:request', { email: e, purpose: 'reauth' }));
      const res = await reqFn(addr);
      challengeId = res?.challengeId || res?.id || '';
      otpTrack.hidden = false;
      otpTrack.style.display = '';
      verifyBtn.hidden = false;
      verifyBtn.style.display = '';
      if (res?.devOtpCode) {
        codeInput.value = String(res.devOtpCode).slice(0, 6);
        syncSlots();
      }
      haptic('selection');
      startCooldown(Number(res?.cooldownSeconds || res?.retryAfterSec || 60));
      codeInput.focus({ preventScroll: true });
    } catch (err) {
      haptic('warning');
      sendBtn.disabled = false;
      sendBtn.textContent = '发送安全验证码';
      errorEl.textContent = err?.message || friendlyMessage(err?.code, err?.status) || '验证码没寄出去，稍后再试';
    }
  });

  verifyBtn.addEventListener('click', async () => {
    const code = codeInput.value.replace(/\D/g, '');
    if (code.length < 6) {
      haptic('warning');
      errorEl.textContent = '请输入邮件里的六位安全验证码';
      return;
    }
    errorEl.textContent = '';
    verifyBtn.disabled = true;
    verifyBtn.textContent = '正在核验…';
    try {
      const verifyFn = api.verifyReauthOtp || ((cid, e, c) => client.post('/auth/email/otp:verify', { challengeId: cid, email: e, code: c, purpose: 'reauth' }));
      const res = await verifyFn(challengeId, emailInput.value.trim(), code);
      const token = res?.reauthToken || res?.token || '';
      if (!token) throw new Error('验证凭证为空，请重试');
      clearInterval(cdTimer);
      haptic('success');
      await onVerified(token);
    } catch (err) {
      // Never wipe codeInput on wrong code (J1-S1 parity)
      haptic('warning');
      verifyBtn.disabled = false;
      verifyBtn.textContent = `验证并${purposeLabel}`;
      errorEl.textContent = err?.message || friendlyMessage(err?.code, err?.status) || '验证码不对或已过期，再试一次';
    }
  });

  wrap.append(
    h('p', { class: 'sheet-desc', text: `为保护你的私密手帐，${purposeLabel}前需再确认一次身份` }),
    emailInput,
    sendBtn,
    otpTrack,
    verifyBtn,
    errorEl,
  );
  return wrap;
}

function renderExportReceipt(host, res, blob, filename) {
  clear(host);
  const stamp = todayCnStamp();
  const title = `心络手帐备份 · ${stamp.cn}`;
  const shareAction = async () => {
    haptic('light');
    if (blob) {
      await shareFileBlob(blob, filename, { title, mimeType: 'application/zip' });
      whisper('已调起系统分享 / 存储到文件', 2400);
    }
  };
  const sha = String(res?.sha256Hex || '—');
  host.append(
    h(
      'div',
      { class: 'receipt-card' },
      h('h4', { class: 'receipt-title', text: '全部手帐已打包完成' }),
      h(
        'dl',
        { class: 'receipt-meta' },
        h('dt', { text: '文件名' }),
        h('dd', { text: filename }),
        h('dt', { text: '压缩包大小' }),
        h('dd', { text: formatBytes(res?.byteSize || blob?.size) }),
        h('dt', { text: 'SHA-256 校验值' }),
        h('dd', { class: 'receipt-mono', text: sha }),
        res?.expiresAt ? [h('dt', { text: '下载有效期至' }), h('dd', { text: formatCnDateTime(res.expiresAt) })] : null,
      ),
      h(
        'div',
        { class: 'receipt-actions' },
        h('button', { type: 'button', class: 'sheet-primary-btn', text: isNativeIos() ? '再次存到「文件」或隔空投送' : '重新下载 .zip', onClick: shareAction }),
        sha !== '—'
          ? h('button', {
              type: 'button',
              class: 'sheet-secondary-btn',
              text: '复制校验值',
              onClick: async () => {
                await copyText(sha);
                haptic('selection');
                whisper('已复制 SHA-256 校验值', 1800);
              },
            })
          : null,
      ),
    ),
  );
}

function renderShredReceipt(host, res, onAccountDeleted) {
  clear(host);
  host.append(
    h(
      'div',
      { class: 'receipt-card is-shredded' },
      h('h4', { class: 'receipt-title', text: '彻底粉碎回执 · 账号与数据已销毁' }),
      h(
        'dl',
        { class: 'receipt-meta' },
        h('dt', { text: '回执编号' }),
        h('dd', { class: 'receipt-mono', text: String(res?.auditReceiptId || '—') }),
        h('dt', { text: '粉碎时间' }),
        h('dd', { text: formatCnDateTime(res?.deletedAt || new Date().toISOString()) }),
        h('dt', { text: '已清除媒体与任务' }),
        h('dd', { text: `${Number(res?.deletedBlobsCount || 0)} 份媒体 · ${Number(res?.cancelledJobsCount || 0)} 项待办` }),
      ),
      h('button', {
        type: 'button',
        class: 'sheet-primary-btn',
        text: '完成并离开心络',
        onClick: () => {
          haptic('light');
          closeSheet({ silentHaptic: true });
          onAccountDeleted?.();
        },
      }),
    ),
  );
}

/** Open the serene Porcelain Data Rights sheet (`mode: 'export' | 'delete'`). */
export function openDataRights(mode = 'export', { onAccountDeleted } = {}) {
  const host = h('div', { class: 'data-rights-host' });

  async function runExportFlow(reauthToken) {
    clear(host);
    host.append(h('p', { class: 'sheet-desc', text: '正在加密打包你的全部文字、日程与相册，请稍候…' }));
    const createFn = api.createExport || ((tok) => client.post('/me/exports', {}, { headers: { 'X-Reauth-Token': tok } }));
    const res = await createFn(reauthToken);
    if (!res?.downloadPath) throw new Error('导出包路径未返回，请稍后再试');
    const dlFn = api.downloadExportBlob || ((p) => client.request('GET', p, { raw: true }));
    const rawRes = await dlFn(res.downloadPath);
    const blob = rawRes instanceof Blob ? rawRes : await rawRes.blob();
    const stamp = todayCnStamp();
    const filename = `mindweave-export-${stamp.iso}.zip`;
    await shareFileBlob(blob, filename, { title: `心络手帐备份 · ${stamp.cn}`, mimeType: 'application/zip' });
    haptic('memorySeal', { tier: 'gold' });
    renderExportReceipt(host, res, blob, filename);
  }

  function showExportView() {
    clear(host);
    host.append(
      buildReauthPanel({
        purposeLabel: '导出全部手帐',
        onVerified: (token) => runExportFlow(token),
      }),
    );
  }

  function showDeleteView() {
    clear(host);
    const confirmInput = h('input', {
      class: 'mw-sheet-input',
      type: 'text',
      placeholder: '请输入「确认删除」四字',
      'aria-label': '输入确认删除以继续',
      autocomplete: 'off',
    });
    const nextBtn = h('button', { type: 'button', class: 'sheet-danger-btn', text: '永久删除账号', disabled: true });
    confirmInput.addEventListener('input', () => {
      nextBtn.disabled = confirmInput.value.trim() !== '确认删除';
    });
    nextBtn.addEventListener('click', async () => {
      const sheetRes = await mwNativeUI.showSheet({
        style: 'actionSheet',
        title: '确认彻底注销并粉碎所有数据？',
        message: '所有人物手帐、照片、语音与云端密钥将被永久销毁，无法恢复。',
        items: [
          { id: 'confirm_delete', label: '永久删除账号', role: 'destructive' },
          { id: 'cancel', label: '取消', role: 'cancel' },
        ],
      });
      if (sheetRes?.selectedId !== 'confirm_delete') return;
      clear(host);
      host.append(
        buildReauthPanel({
          purposeLabel: '永久删除账号',
          onVerified: async (reauthToken) => {
            const delFn = api.deleteAccount || ((tok) => client.request('DELETE', '/me', { headers: { 'X-Reauth-Token': tok } }));
            const res = await delFn(reauthToken);
            try {
              localStorage.clear();
            } catch {
              /* ignore */
            }
            if (isNativeIos()) mwBiometrics?.setEnabled?.(false).catch(() => {});
            client.setTokens(null);
            haptic('rigid');
            renderShredReceipt(host, res, onAccountDeleted);
          },
        }),
      );
    });

    host.append(
      h('p', { class: 'sheet-desc', text: '注销后，你在心络的全部文字、照片、语音与加密密钥将被即时粉碎，无法找回。' }),
      h('button', {
        type: 'button',
        class: 'sheet-secondary-btn',
        text: '删除前先导出全部手帐',
        onClick: () => {
          haptic('light');
          showExportView();
        },
      }),
      confirmInput,
      nextBtn,
    );
  }

  if (mode === 'delete') showDeleteView();
  else showExportView();

  openSheet({
    title: mode === 'delete' ? '注销账号并彻底粉碎数据' : '导出全部手帐',
    body: host,
  });
}
