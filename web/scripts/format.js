// format.js — Pure formatting, canonical V2 tier labels, dual lunar/solar dates & safe search highlighters (<= 300L).

export function shouldSendOnEnter(e) {
  return Boolean(e && e.key === 'Enter' && !e.isComposing && e.keyCode !== 229 && !e.shiftKey);
}

export function formatDateCn(value) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value || ''));
  return m ? `${Number(m[2])}月${Number(m[3])}日` : '';
}

export function formatClock(value) {
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? '' : `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

export function todayIso() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

export function citationLabel(c) {
  const title = String(c?.title || c?.snippet || '一页手帐').trim();
  const chars = Array.from(title);
  const short = chars.slice(0, 14).join('') + (chars.length > 14 ? '…' : '');
  const date = formatDateCn(c?.happenedOn);
  return date ? `${short} · ${date}` : short;
}

export const TIER_LABEL = Object.freeze({
  gold: Object.freeze({ arrow: '金箭', short: '珍重', meaning: '最珍视的人，丘比特会多替你想一步' }),
  silver: Object.freeze({ arrow: '银盾', short: '边界', meaning: '冷静留存客观事实与边界，默认不存照片' }),
  crystal: Object.freeze({ arrow: '水晶', short: '日常', meaning: '日常往来的人，礼数周全不耗神' }),
});

export const REMINDER_STATUS_CN = Object.freeze({
  active: '提醒中', paused: '已暂停', proposed: '待确认', pending: '待确认', completed: '已完成', done: '已完成',
});

export const namesList = (names) => (names || []).map((n) => `「${n}」`).join('');

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

export function formatDateFullCn(value) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value || ''));
  return m ? `${m[1]}年${Number(m[2])}月${Number(m[3])}日` : '';
}

export function monthKeyOf(value) {
  const m = /^(\d{4})-(\d{2})/.exec(String(value || ''));
  return m ? `${m[1]}-${m[2]}` : '';
}

export function monthNameCn(ym, { includeYear = false } = {}) {
  if (!ym || ym === 'earlier') return '更早';
  const m = /^(\d{4})-(\d{2})$/.exec(String(ym));
  if (!m) return String(ym);
  const name = MONTH_CN[Number(m[2])] || `${Number(m[2])}月`;
  return includeYear && Number(m[1]) !== new Date().getFullYear() ? `${m[1]} · ${name}` : name;
}

export function monthShortCn(ym) {
  if (!ym || ym === 'earlier') return '更早';
  const m = /^(\d{4})-(\d{2})$/.exec(String(ym));
  return m ? `${Number(m[2])}月` : String(ym);
}

export function formatLunarCn(month, day, isLeap = false) {
  const mm = LUNAR_MONTH_CN[Number(month)] || (month ? `${month}月` : '');
  const dd = LUNAR_DAY_CN[Number(day)] || (day ? `${day}日` : '');
  return !mm && !dd ? '' : `农历${isLeap ? '闰' : ''}${mm}${dd}`;
}

export function formatFactDateCn(fact) {
  if (!fact) return '';
  const lm = fact.lunarMonth ?? fact.targetMonth;
  const ld = fact.lunarDay ?? fact.targetDay;
  if (fact.isLunar && lm && ld) {
    const lunar = formatLunarCn(lm, ld, fact.lunarIsLeapMonth ?? fact.isLeapMonth);
    const solar = formatDateCn(fact.nextOccurrenceDate || fact.triggerDate);
    return solar ? `${lunar} · 公历${solar}` : lunar;
  }
  return formatDateCn(fact.triggerDate || fact.nextOccurrenceDate);
}

export function formatReminderDateCn(rem) {
  if (!rem) return '';
  const lm = rem.targetMonth ?? rem.lunarMonth;
  const ld = rem.targetDay ?? rem.lunarDay;
  if (rem.isLunar && lm && ld) {
    const lunar = formatLunarCn(lm, ld, rem.isLeapMonth ?? rem.lunarIsLeapMonth);
    const solar = formatDateCn(rem.nextOccurrenceDate || rem.nextFireAt);
    return solar ? `${lunar} · 公历${solar}` : lunar;
  }
  if (rem.rule === 'yearly' && lm && ld) return `每年 ${lm}月${ld}日`;
  return formatDateCn(rem.onceDate || rem.nextOccurrenceDate || rem.triggerDate || rem.nextFireAt);
}

export const highlightParts = (text, query) => highlightMatches(text, query, []);

/** Multi-term case-insensitive highlighter returning safe [{ text, hit }] spans (zero HTML). */
export function highlightMatches(text, query, matchedTerms = []) {
  const source = String(text ?? '');
  if (!source) return [{ text: '', hit: false }];
  const rawTerms = [];
  if (Array.isArray(matchedTerms)) for (const t of matchedTerms) { const c = String(t ?? '').trim(); if (c) rawTerms.push(c); }
  const q = String(query ?? '').trim();
  if (q) rawTerms.push(q);
  const unique = [...new Set(rawTerms.map((t) => t.toLowerCase()))].sort((a, b) => b.length - a.length);
  if (!unique.length) return [{ text: source, hit: false }];
  const lower = source.toLowerCase();
  const spans = [];
  for (const needle of unique) {
    let pos = 0;
    let idx;
    while ((idx = lower.indexOf(needle, pos)) !== -1) { spans.push([idx, idx + needle.length]); pos = idx + needle.length; }
  }
  if (!spans.length) return [{ text: source, hit: false }];
  spans.sort((a, b) => (a[0] - b[0]) || (b[1] - a[1]));
  const merged = [];
  for (const [s, e] of spans) {
    if (!merged.length || s > merged[merged.length - 1][1]) merged.push([s, e]);
    else if (e > merged[merged.length - 1][1]) merged[merged.length - 1][1] = e;
  }
  const parts = [];
  let cursor = 0;
  for (const [s, e] of merged) {
    if (s > cursor) parts.push({ text: source.slice(cursor, s), hit: false });
    parts.push({ text: source.slice(s, e), hit: true });
    cursor = e;
  }
  if (cursor < source.length) parts.push({ text: source.slice(cursor), hit: false });
  return parts;
}

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
  return keys.map((ym) => ({ ym, label: monthNameCn(ym, { includeYear: multiYear }), shortLabel: monthShortCn(ym), items: map.get(ym) }));
}

export function matchesMaterialQuery(m, q) {
  const needle = String(q || '').trim().toLowerCase();
  if (!needle) return true;
  const hay = [m?.body, m?.snippet, formatDateCn(m?.happenedOn || m?.createdAt), formatDateFullCn(m?.happenedOn || m?.createdAt)]
    .filter(Boolean).join(' ').toLowerCase();
  return hay.includes(needle);
}

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
      id: r.id, when: formatReminderDateCn(r) || formatFactDateCn(linked), text: r.title || linked?.content || '',
      recordId: linked?.recordId || null, status: r.status, statusCn: REMINDER_STATUS_CN[r.status] || '',
      pending: r.status === 'proposed', reminder: r,
    });
  }
  for (const f of facts) {
    if (used.has(f.id)) continue;
    if (f.type === 'date' || f.triggerDate || f.isLunar) {
      used.add(f.id);
      dateLines.push({ when: formatFactDateCn(f), text: f.content || '', recordId: f.recordId || null, pending: f.status === 'pending' });
    }
  }
  const detailLines = facts.filter((f) => !used.has(f.id)).map((f) => ({ text: f.content || '', recordId: f.recordId || null, pending: f.status === 'pending' }));
  return { dateLines, detailLines };
}

const TRACE_UUID_RE = /[0-9a-f]{8}-[0-9a-f]{4}(?:-[0-9a-f]{4}){0,2}(?:-[0-9a-f]{12})?/gi;
const cleanTraceText = (raw) => String(raw ?? '').replace(TRACE_UUID_RE, '').replace(/\s+/g, ' ').trim();

function numericCount(step, pattern) {
  if (typeof step?.count === 'number' && Number.isFinite(step.count) && step.count >= 0) return Math.floor(step.count);
  const m = pattern.exec(step?.label || '');
  return m ? Number(m[1]) : null;
}

export function sanitizeAgentStep(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const label = cleanTraceText(raw.label);
  if (!label) return null;
  const detail = cleanTraceText(raw.detail);
  const count = typeof raw.count === 'number' && Number.isFinite(raw.count) && raw.count >= 0 ? Math.floor(raw.count) : null;
  return { step: raw.step ?? null, phase: String(raw.phase || '').trim().toLowerCase(), label, detail, count };
}

export function summarizeAgentTrace(rawSteps) {
  const steps = (rawSteps || []).map(sanitizeAgentStep).filter(Boolean);
  if (!steps.length) return '';
  let pages = null; let hits = null; let checked = null;
  for (const s of steps) {
    const p = s.phase;
    const isRecall = /(recall|search|retrieve|match|hit|cite|bm25|evidence)/.test(p) || /原话|引用/.test(s.label);
    const isScan = !isRecall && (/(scan|load|read|material|record|fetch|journal|page)/.test(p) || /翻看|翻阅/.test(s.label));
    const isCheck = !isRecall && !isScan && (/(fact|memory|reminder|schedule|check|profile)/.test(p) || /细节|日程/.test(s.label));
    if (isScan) { const n = numericCount(s, /(\d+)\s*页/); if (n !== null) pages = n; }
    else if (isRecall) { const n = numericCount(s, /(\d+)\s*(?:处|条|页)/); if (n !== null) hits = n; }
    else if (isCheck) { const n = numericCount(s, /(\d+)\s*(?:条|项)/); if (n !== null) checked = n; }
  }
  const parts = [];
  if (pages !== null && pages > 0) parts.push(`翻看 ${pages} 页手帐`);
  else if (pages === 0) parts.push('翻看手帐');
  if (hits !== null && hits > 0) parts.push(`引用 ${hits} 处原话`);
  else if (checked !== null && checked > 0 && parts.length < 2) parts.push(`参考 ${checked} 条细节`);
  if (parts.length) return parts.join(' · ');
  const tail = steps[steps.length - 1].label.replace(/[…\.。]+$/, '').trim();
  return tail || `翻看 ${steps.length} 处手帐细节`;
}

export function formatActionPreview(raw) {
  const cleaned = cleanTraceText(
    String(raw ?? '').replace(/\[(\d{4}-\d{2}-\d{2})\]/g, (_, d) => formatDateCn(d) || d).replace(/\r?\n+\s*(?:[-•·*]\s*)?/g, ' · '),
  );
  return cleaned.replace(/(?:\s*·\s*){2,}/g, ' · ').replace(/^·\s*|\s*·$/g, '').trim();
}
