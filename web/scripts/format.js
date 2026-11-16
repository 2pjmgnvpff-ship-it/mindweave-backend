// format.js — small pure helpers shared by views (unit-tested where it matters).

/** Chinese IME safe Enter: send only when not composing and Shift is not held. */
export function shouldSendOnEnter(e) {
  if (!e || e.key !== 'Enter') return false;
  if (e.isComposing || e.keyCode === 229) return false;
  if (e.shiftKey) return false;
  return true;
}

/** '2026-05-12' → '5月12日'; ISO timestamp works too. */
export function formatDateCn(value) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value || ''));
  if (!m) return '';
  return `${Number(m[2])}月${Number(m[3])}日`;
}

/** '2026-11-16T10:03:00Z' → '10:03' in local time. */
export function formatClock(value) {
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return '';
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

export function todayIso() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

/** Citation pill label: never an id. */
export function citationLabel(c) {
  const title = String(c?.title || c?.snippet || '一页手帐').trim();
  const short = Array.from(title).slice(0, 14).join('') + (Array.from(title).length > 14 ? '…' : '');
  const date = formatDateCn(c?.happenedOn);
  return date ? `${short} · ${date}` : short;
}

export const TIER_LABEL = {
  gold: { arrow: '金箭', short: '记爱', meaning: '最珍视的人，丘比特会多替你想一步' },
  silver: { arrow: '银箭', short: '记恨', meaning: '需要守好边界的人，照片默认收起' },
  crystal: { arrow: '水晶箭', short: '记繁琐', meaning: '日常往来的人，礼数周全不耗神' },
};

export function namesList(names) {
  return names.map((n) => `「${n}」`).join('');
}

/** Collapse whitespace and clip for one-line previews. */
export function clip(text, max = 40) {
  const s = String(text || '').replace(/\s+/g, ' ').trim();
  const chars = Array.from(s);
  return chars.length > max ? chars.slice(0, max).join('') + '…' : s;
}
