// lib/solar-terms.js — Zero-dep 24 Solar Terms (VSOP87D truncated, Meeus 2nd ed.) + ICU lunar day (<= 110L).
const RAD = Math.PI / 180;
const CST_OFFSET_MS = 8 * 3600000;
export const SOLAR_TERM_NAMES = Object.freeze([
  '小寒', '大寒', '立春', '雨水', '惊蛰', '春分', '清明', '谷雨',
  '立夏', '小满', '芒种', '夏至', '小暑', '大暑', '立秋', '处暑',
  '白露', '秋分', '寒露', '霜降', '立冬', '小雪', '大雪', '冬至',
]);
const L0 = [
  [175347046, 0, 0], [3341656, 4.6692568, 6283.07585], [34894, 4.6261, 12566.1517],
  [3497, 2.7441, 5753.3849], [3418, 2.8289, 3.5231], [3136, 3.6277, 77713.7715],
  [2676, 4.4181, 7860.4194], [2343, 6.1352, 3930.2097], [1324, 0.7425, 11506.7698],
  [1273, 2.0371, 529.691], [1199, 1.1096, 1577.3435], [990, 5.233, 5884.927],
  [902, 2.045, 26.298], [857, 3.508, 398.149], [780, 1.179, 5223.694],
  [753, 2.533, 5507.553], [505, 4.583, 18849.228], [492, 4.205, 775.523],
];
const L1 = [
  [628331966747, 0, 0], [206059, 2.678235, 6283.07585], [4303, 2.6351, 12566.1517],
  [425, 1.59, 3.523], [119, 5.796, 26.298], [109, 2.966, 1577.344],
];
const L2 = [[52919, 0, 0], [8720, 1.0721, 6283.0758], [309, 0.867, 12566.152]];
const yearCache = new Map();

function sumSeries(tbl, tau) {
  let s = 0;
  for (let i = 0; i < tbl.length; i++) s += tbl[i][0] * Math.cos(tbl[i][1] + tbl[i][2] * tau);
  return s;
}

function apparentSolarLon(jde) {
  const tau = (jde - 2451545.0) / 365250.0;
  const T = tau * 10;
  const Lrad = (sumSeries(L0, tau) + tau * (sumSeries(L1, tau) + tau * sumSeries(L2, tau))) * 1e-8;
  const theta = ((Lrad / RAD + 180) % 360 + 360) % 360;
  const om = (125.04452 - 1934.136261 * T) * RAD;
  const Lsun = (280.4665 + 36000.7698 * T) * RAD;
  const Lmoon = (218.3165 + 481267.8813 * T) * RAD;
  const corr = (-20.58013 - 17.20 * Math.sin(om) - 1.32 * Math.sin(2 * Lsun) - 0.23 * Math.sin(2 * Lmoon) + 0.21 * Math.sin(2 * om)) / 3600;
  return ((theta + corr) % 360 + 360) % 360;
}

function deltaTDays(year) {
  if (year >= 2005 && year < 2050) { const t = year - 2000; return (62.92 + 0.32217 * t + 0.005589 * t * t) / 86400; }
  const u = (year - 1820) / 100;
  return (year >= 2050 ? -20 + 32 * u * u - 0.5628 * (2150 - year) : -20 + 32 * u * u) / 86400;
}

export function getYearSolarTerms(year) {
  const y = Number(year);
  if (yearCache.has(y)) return yearCache.get(y);
  const dt = deltaTDays(y);
  const list = new Array(24);
  const byIso = new Map();
  const byMonth = new Map();
  for (let idx = 0; idx < 24; idx++) {
    const target = (285 + idx * 15) % 360;
    let jde = 2451545.0 + (y - 2000) * 365.2422 + 5.5 + idx * 15.2184;
    for (let k = 0; k < 4; k++) {
      let diff = target - apparentSolarLon(jde);
      if (diff > 180) diff -= 360; else if (diff < -180) diff += 360;
      jde += diff * (365.2422 / 360);
      if (Math.abs(diff) < 1e-4) break;
    }
    const d = new Date(Math.round((jde - dt - 2440587.5) * 86400000 + CST_OFFSET_MS));
    const month = d.getUTCMonth() + 1;
    const day = d.getUTCDate();
    const iso = `${y}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
    const term = { idx, name: SOLAR_TERM_NAMES[idx], month, day, iso };
    list[idx] = term;
    byIso.set(iso, term);
    if (!byMonth.has(month)) byMonth.set(month, []);
    byMonth.get(month).push(term);
  }
  const entry = { list, byIso, byMonth };
  yearCache.set(y, entry);
  return entry;
}

export function solarTermOnDate(isoDate) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(isoDate || ''));
  return m ? (getYearSolarTerms(Number(m[1])).byIso.get(`${m[1]}-${m[2]}-${m[3]}`)?.name || '') : '';
}

export function monthSolarTerms(ym) {
  const m = /^(\d{4})-(\d{1,2})$/.exec(String(ym || ''));
  return m ? (getYearSolarTerms(Number(m[1])).byMonth.get(Number(m[2])) || []) : [];
}

export function getSolarTermForMonth(year, month) {
  return (getYearSolarTerms(Number(year)).byMonth.get(Number(month)) || [])[0]?.name || '';
}

let lunarFmt = null;
export function lunarShortCn(isoDate) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(isoDate || ''));
  if (!m) return '';
  try {
    lunarFmt ||= new Intl.DateTimeFormat('zh-CN-u-ca-chinese', { month: 'long', day: 'numeric' });
    return lunarFmt.format(new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])));
  } catch { return ''; }
}

export function formatLunarDateCn(dateObj) {
  const iso = dateObj instanceof Date && !Number.isNaN(dateObj.getTime())
    ? `${dateObj.getFullYear()}-${String(dateObj.getMonth() + 1).padStart(2, '0')}-${String(dateObj.getDate()).padStart(2, '0')}`
    : String(dateObj || '');
  const raw = lunarShortCn(iso);
  return !raw ? '' : (raw.startsWith('农历') ? raw : `农历${raw}`);
}
