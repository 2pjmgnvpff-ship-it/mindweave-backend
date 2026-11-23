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

const MONTH_CN = ['', '一月', '二月', '三月', '四月', '五月', '六月', '七月', '八月', '九月', '十月', '十一月', '十二月'];
const LUNAR_MONTH_CN = ['', '正月', '二月', '三月', '四月', '五月', '六月', '七月', '八月', '九月', '十月', '冬月', '腊月'];
const LUNAR_DAY_CN = [
  '', '初一', '初二', '初三', '初四', '初五', '初六', '初七', '初八', '初九', '初十',
  '十一', '十二', '十三', '十四', '十五', '十六', '十七', '十八', '十九', '二十',
  '廿一', '廿二', '廿三', '廿四', '廿五', '廿六', '廿七', '廿八', '廿九', '三十',
];

/** '2026-11-16' → '2026年11月16日'. */
export function formatDateFullCn(value) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value || ''));
  if (!m) return '';
  return `${m[1]}年${Number(m[2])}月${Number(m[3])}日`;
}

/** '2026-11-16' or ISO → '2026-11', else ''. */
export function monthKeyOf(value) {
  const m = /^(\d{4})-(\d{2})/.exec(String(value || ''));
  return m ? `${m[1]}-${m[2]}` : '';
}

/** '2026-11' → '十一月' (or '2025年 · 十二月' when cross-year if includeYear=true). */
export function monthNameCn(ym, { includeYear = false } = {}) {
  if (!ym || ym === 'earlier') return '更早';
  const m = /^(\d{4})-(\d{2})$/.exec(String(ym));
  if (!m) return String(ym);
  const name = MONTH_CN[Number(m[2])] || `${Number(m[2])}月`;
  if (includeYear && Number(m[1]) !== new Date().getFullYear()) return `${m[1]} · ${name}`;
  return name;
}

/** '2026-11' → '11月' for compact right-edge physical paper tabs. */
export function monthShortCn(ym) {
  if (!ym || ym === 'earlier') return '更早';
  const m = /^(\d{4})-(\d{2})$/.exec(String(ym));
  return m ? `${Number(m[2])}月` : String(ym);
}

/** Format a lunar month/day pair cleanly, e.g. '农历八月十五'. */
export function formatLunarCn(month, day, isLeap = false) {
  const mm = LUNAR_MONTH_CN[Number(month)] || (month ? `${month}月` : '');
  const dd = LUNAR_DAY_CN[Number(day)] || (day ? `${day}日` : '');
  if (!mm && !dd) return '';
  return `农历${isLeap ? '闰' : ''}${mm}${dd}`;
}

export function formatFactDateCn(fact) {
  if (!fact) return '';
  if (fact.isLunar && fact.lunarMonth && fact.lunarDay) {
    return formatLunarCn(fact.lunarMonth, fact.lunarDay, fact.lunarIsLeapMonth);
  }
  return formatDateCn(fact.triggerDate);
}

export function formatReminderDateCn(rem) {
  if (!rem) return '';
  if (rem.isLunar && rem.targetMonth && rem.targetDay) {
    return formatLunarCn(rem.targetMonth, rem.targetDay, rem.isLeapMonth);
  }
  if (rem.rule === 'yearly' && rem.targetMonth && rem.targetDay) {
    return `每年 ${rem.targetMonth}月${rem.targetDay}日`;
  }
  return formatDateCn(rem.onceDate || rem.nextFireAt);
}

/** Split text into safe segments for DOM <mark> highlighting (case-insensitive, zero HTML). */
export function highlightParts(text, query) {
  const source = String(text ?? '');
  const q = String(query ?? '').trim();
  if (!q) return [{ text: source, hit: false }];
  const lower = source.toLowerCase();
  const needle = q.toLowerCase();
  const parts = [];
  let pos = 0;
  let idx;
  while ((idx = lower.indexOf(needle, pos)) !== -1) {
    if (idx > pos) parts.push({ text: source.slice(pos, idx), hit: false });
    parts.push({ text: source.slice(idx, idx + q.length), hit: true });
    pos = idx + q.length;
  }
  if (pos < source.length) parts.push({ text: source.slice(pos), hit: false });
  return parts.length ? parts : [{ text: source, hit: false }];
}

/** Group materials descending by YYYY-MM month key (or 'earlier'). */
export function groupMaterialsByMonth(items) {
  const map = new Map();
  for (const m of items || []) {
    const ym = monthKeyOf(m.happenedOn || m.createdAt) || 'earlier';
    if (!map.has(ym)) map.set(ym, []);
    map.get(ym).push(m);
  }
  const keys = [...map.keys()].sort((a, b) => (a === 'earlier' ? 1 : b === 'earlier' ? -1 : b.localeCompare(a)));
  const years = new Set(keys.filter((k) => k !== 'earlier').map((k) => k.slice(0, 4)));
  const multiYear = years.size > 1 || [...years].some((y) => Number(y) !== new Date().getFullYear());
  return keys.map((ym) => ({
    ym,
    label: monthNameCn(ym, { includeYear: multiYear }),
    shortLabel: monthShortCn(ym),
    items: map.get(ym),
  }));
}

/** Case-insensitive match for Spotlight Search across body, snippet, and formatted date. */
export function matchesMaterialQuery(m, q) {
  const needle = String(q || '').trim().toLowerCase();
  if (!needle) return true;
  const hay = [m?.body, m?.snippet, formatDateCn(m?.happenedOn || m?.createdAt), formatDateFullCn(m?.happenedOn || m?.createdAt)]
    .filter(Boolean)
    .join(' ')
    .toLowerCase();
  return hay.includes(needle);
}

/** Deduplicate real facts + reminders into serene Cover Vellum Pocket lines. */
export function buildVellumSections(rawFacts, rawReminders) {
  const facts = (rawFacts || []).filter((f) => f && f.status !== 'rejected');
  const reminders = (rawReminders || []).filter((r) => r && r.status !== 'cancelled' && r.status !== 'completed');
  const factById = new Map(facts.map((f) => [f.id, f]));
  const used = new Set();
  const dateLines = [];

  for (const r of reminders) {
    const linked = r.factId ? factById.get(r.factId) : null;
    if (linked) used.add(linked.id);
    dateLines.push({
      when: formatReminderDateCn(r) || formatFactDateCn(linked),
      text: r.title || linked?.content || '',
      recordId: linked?.recordId || null,
      pending: r.status === 'proposed',
    });
  }
  for (const f of facts) {
    if (used.has(f.id)) continue;
    if (f.type === 'date' || f.triggerDate || f.isLunar) {
      used.add(f.id);
      dateLines.push({
        when: formatFactDateCn(f),
        text: f.content || '',
        recordId: f.recordId || null,
        pending: f.status === 'pending',
      });
    }
  }
  const detailLines = facts
    .filter((f) => !used.has(f.id))
    .map((f) => ({ text: f.content || '', recordId: f.recordId || null, pending: f.status === 'pending' }));
  return { dateLines, detailLines };
}
