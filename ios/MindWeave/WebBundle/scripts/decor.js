// decor.js — Deterministic porcelain monogram & tier palette helper (zero card tilts, zero washi, <= 140L).

export const TIERS = ['gold', 'silver', 'crystal'];

export const PALETTE = Object.freeze({
  seal: Object.freeze({
    gold: [['#FAF2E6', '#9A4E24'], ['#F8EFE2', '#8A6518'], ['#FBF4EA', '#9A4E24']],
    silver: [['#EEF2F7', '#415C80'], ['#E8EEF5', '#3B5475'], ['#F1F4F8', '#415C80']],
    crystal: [['#EDF4FC', '#335D96'], ['#E9F1FB', '#2E5488'], ['#F0F6FD', '#335D96']],
  }),
  tab: Object.freeze({
    gold: ['#D98A5C', '#C49B45', '#E7C3A4'],
    silver: ['#617996', '#788CA6', '#9EB0C6'],
    crystal: ['#5C89C6', '#6A94CC', '#8FB8E6'],
  }),
});

const WEEKDAYS_CN = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

export function hash32(input) {
  const str = String(input ?? '');
  let h = 0x811c9dc5;
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h >>> 0;
}

export function seededRandom(seed) {
  let a = seed >>> 0;
  return function next() {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function parseDay(date) {
  if (date instanceof Date && !Number.isNaN(date.getTime())) {
    return { y: date.getFullYear(), m: date.getMonth() + 1, d: date.getDate() };
  }
  const s = String(date ?? '');
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(s);
  if (match) return { y: Number(match[1]), m: Number(match[2]), d: Number(match[3]) };
  return null;
}

const pad2 = (n) => String(n).padStart(2, '0');
const pick = (list, r) => list[Math.floor(r() * list.length) % list.length];
const normTier = (tier) => (TIERS.includes(tier) ? tier : 'crystal');

export function decorFor({ id, date, tier } = {}) {
  const t = normTier(tier);
  const day = parseDay(date) || { y: 2026, m: 1, d: 1 };
  const seed = hash32(`${id}|${day.y}-${pad2(day.m)}-${pad2(day.d)}|${t}`);
  const r = seededRandom(seed);
  const mmdd = `${pad2(day.m)}${pad2(day.d)}`;
  const letter = 'ABCDEFGHJK'[Math.floor(r() * 10)];
  const frame = 1 + Math.floor(r() * 36);
  const [sealBg, sealColor] = pick(PALETTE.seal[t], r);
  const weekday = WEEKDAYS_CN[new Date(Date.UTC(day.y, day.m - 1, day.d)).getUTCDay()];
  return {
    ticketNo: `№ ${mmdd}-${letter}`,
    filmCaption: `胶片 · 第 ${frame} 格`,
    stampCode: `心络 · ${pad2(day.m)}.${pad2(day.d)}`,
    folioDate: `${pad2(day.m)}.${pad2(day.d)}`,
    hoboDay: String(day.d),
    hoboMonthWk: `${day.m}月 · ${weekday}`,
    sealBg,
    sealColor,
    tabColor: pick(PALETTE.tab[t], r),
    rotation: 0,
  };
}

export function materialDecor(material, tier) {
  const t = normTier(tier);
  const r = seededRandom(hash32(`m|${material?.id ?? ''}|${t}`));
  return {
    rotation: 0,
    side: r() < 0.5 ? 'left' : 'right',
    accentColor: pick(PALETTE.tab[t], r),
  };
}

const SAFE_COLOR = /^(#[0-9a-f]{3,8}|rgba?\(\s*[\d.]+%?\s*,\s*[\d.]+%?\s*,\s*[\d.]+%?\s*(,\s*[\d.]+\s*)?\)|linear-gradient\([#\w\s.,%()-]+\))$/i;

export function safeColor(value, fallback) {
  const v = typeof value === 'string' ? value.trim() : '';
  return v && v.length < 160 && SAFE_COLOR.test(v) ? v : fallback;
}

export function personDecor(person, date) {
  const base = decorFor({ id: person?.id, date, tier: person?.tier });
  return {
    ...base,
    sealBg: safeColor(person?.sealBg, base.sealBg),
    sealColor: safeColor(person?.sealColor, base.sealColor),
    tabColor: safeColor(person?.tabColor, base.tabColor),
  };
}

export function monogramOf(person) {
  const m = String(person?.monogram || '').trim();
  if (m) return Array.from(m)[0];
  const n = String(person?.nameCn || '').trim();
  return n ? Array.from(n)[0] : '·';
}

export function sealStyle(person, date) {
  const d = personDecor(person, date);
  return { background: d.sealBg, color: d.sealColor };
}

export function isShortNoteSlip(m, tier) {
  if (!m || m.presentation === 'question' || m.type === 'voice') return false;
  if (m.type === 'photo' && m.mediaUrl && tier !== 'silver') return false;
  const len = Array.from(String(m.body || m.snippet || '').trim()).length;
  return len > 0 && len <= 28;
}

export function gentleTilt() {
  return 0;
}
