// decor.js — D2 deterministic decoration generator.
// Same (id, date, tier) → same ticket number, film caption, stamp, tape & seal colours.
// Only palette tones that already exist in the V10 reference / design tokens are used.

export const TIERS = ['gold', 'silver', 'crystal'];

export const PALETTE = Object.freeze({
  tape: Object.freeze({
    gold: ['rgba(238, 198, 152, 0.72)', 'rgba(232, 182, 98, 0.72)', 'rgba(246, 214, 176, 0.78)', 'rgba(248, 201, 188, 0.8)'],
    silver: ['rgba(142, 168, 194, 0.74)', 'rgba(182, 204, 224, 0.78)', 'rgba(191, 224, 210, 0.8)', 'rgba(214, 206, 196, 0.8)'],
    crystal: ['rgba(114, 204, 222, 0.72)', 'rgba(188, 176, 242, 0.72)', 'rgba(191, 224, 210, 0.82)', 'rgba(164, 228, 242, 0.7)'],
  }),
  seal: Object.freeze({
    gold: [['#fbeed6', '#966418'], ['#f9f3ea', '#8a5a14'], ['#f6e6cc', '#9e6b1e']],
    silver: [['#eef3f8', '#46607a'], ['#e4edf6', '#394e63'], ['#ebeff3', '#50667d']],
    crystal: [['#eaf7fb', '#2f7385'], ['#ece8fb', '#3d4e88'], ['#e6f4f8', '#346c7d']],
  }),
  tab: Object.freeze({
    gold: ['#e8b662', '#d99b38', '#f0c98a'],
    silver: ['#8fa6bd', '#a9bccf', '#7a93ab'],
    crystal: ['#72ccde', '#a991f0', '#8fd3e6'],
  }),
});

const WEEKDAYS_CN = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

/** FNV-1a 32-bit hash. */
export function hash32(input) {
  const str = String(input ?? '');
  let h = 0x811c9dc5;
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h >>> 0;
}

/** mulberry32 seeded PRNG → function returning [0, 1). */
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

/** Parse 'YYYY-MM-DD' or an ISO timestamp or a Date into {y, m, d} without timezone drift. */
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

function pick(list, r) {
  return list[Math.floor(r() * list.length) % list.length];
}

function normTier(tier) {
  return TIERS.includes(tier) ? tier : 'crystal';
}

/**
 * Decoration for a person page / capsule / directory row.
 * @param {{id:string, date?:string|Date|null, tier?:string}} input
 */
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
    tapeColor: pick(PALETTE.tape[t], r),
    sealBg,
    sealColor,
    tabColor: pick(PALETTE.tab[t], r),
    rotation: Number(((r() - 0.5) * 5).toFixed(2)),
  };
}

/** Per-material decoration: rotation, side, tape tone. Deterministic from material id. */
export function materialDecor(material, tier) {
  const t = normTier(tier);
  const r = seededRandom(hash32(`m|${material?.id ?? ''}|${t}`));
  return {
    rotation: Number(((r() - 0.5) * 4.4).toFixed(2)),
    side: r() < 0.5 ? 'left' : 'right',
    tapeColor: pick(PALETTE.tape[t], r),
    tapeShift: Math.round(16 + r() * 60),
  };
}

const SAFE_COLOR = /^(#[0-9a-f]{3,8}|rgba?\(\s*[\d.]+%?\s*,\s*[\d.]+%?\s*,\s*[\d.]+%?\s*(,\s*[\d.]+\s*)?\)|linear-gradient\([#\w\s.,%()-]+\))$/i;

/** Accept a server-provided colour only if it is a plain colour / gradient; otherwise fall back. */
export function safeColor(value, fallback) {
  const v = typeof value === 'string' ? value.trim() : '';
  return v && v.length < 160 && SAFE_COLOR.test(v) ? v : fallback;
}

/** Merge server visual hints with deterministic decor (server wins if it is a safe colour). */
export function personDecor(person, date) {
  const base = decorFor({ id: person?.id, date, tier: person?.tier });
  return {
    ...base,
    sealBg: safeColor(person?.sealBg, base.sealBg),
    sealColor: safeColor(person?.sealColor, base.sealColor),
    tapeColor: safeColor(person?.tapeColor, base.tapeColor),
    tabColor: safeColor(person?.tabColor, base.tabColor),
  };
}

export function monogramOf(person) {
  const m = String(person?.monogram || '').trim();
  if (m) return Array.from(m)[0];
  const n = String(person?.nameCn || '').trim();
  return n ? Array.from(n)[0] : '·';
}
