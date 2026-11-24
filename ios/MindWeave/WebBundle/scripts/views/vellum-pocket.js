// vellum-pocket.js — Calm Upcoming Horizon Ribbon (<= 14d, 0 red badges, 0 sparkle, Silver birthdays paused)
// + Cover Vellum Pocket (记挂的日子 / 重要约定 / 关键节点) + native UNUserNotificationCenter sync.
import { h, clear } from '../dom.js';
import { api } from '../api/endpoints.js';
import { formatDateCn, formatFactDateCn, formatReminderDateCn } from '../format.js';
import { openSheet, closeSheet } from './sheet.js';
import { showUndoToast, whisper } from './toast.js';
import { actions } from '../actions.js';
import { haptic, mwNativeUI } from '../native-bridge.js';

const BRIEF_CTA = {
  gold: '让丘比特帮我准备 →',
  silver: '让丘比特梳理时间线 →',
  crystal: '让丘比特理清要点 →',
};

const DATE_SECTION_TITLE = {
  gold: '记挂的日子',
  silver: '关键节点',
  crystal: '重要约定',
};

const LEAD_CHOICES = [7, 1, 0];

function daysUntil(isoDate) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(isoDate || ''));
  if (!m) return Infinity;
  const target = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  return Math.round((target - today) / 86_400_000);
}

function dualDateLabel(rem) {
  const base = formatReminderDateCn(rem);
  const nextOcc = rem?.nextOccurrenceDate || rem?.onceDate || rem?.nextFireAt;
  const solar = formatDateCn(nextOcc);
  if (rem?.isLunar && base && solar && !base.includes('公历')) return `${base} · 公历 ${solar}`;
  return base || solar || '';
}

function syncNativeReminders(person, reminders) {
  if (!mwNativeUI?.syncReminders) return;
  const active = (reminders || [])
    .filter((r) => r && r.status === 'active' && (r.nextFireAt || r.nextOccurrenceDate || r.onceDate))
    .slice(0, 48)
    .map((r) => ({
      id: r.id,
      personId: person?.id || r.personId || '',
      title: '心络 · 今日有一条重要提醒',
      placeholder: '你有一条心络提醒',
      triggerDate: r.nextOccurrenceDate || r.onceDate || r.nextFireAt,
      leadDays: r.leadDays ?? 1,
      badge: null,
    }));
  try { mwNativeUI.syncReminders(active); } catch { /* ignore */ }
}

/**
 * Render Calm Upcoming Horizon Ribbon above Month 1 ONLY when active reminders fall within [-2d, +14d].
 * 48h anti-guilt retire: reminders overdue > 2 days quietly retire without red badges.
 */
export function renderUpcomingRibbon(person, reminders, { onOpenVellum, onMutated } = {}) {
  if (!person || !Array.isArray(reminders)) return null;
  const tier = person.tier || 'crystal';
  const upcoming = reminders
    .filter((r) => {
      if (!r || r.status !== 'active') return false;
      if (tier === 'silver' && /生日|birthday/i.test(String(r.title || r.kind || ''))) return false;
      const d = daysUntil(r.nextOccurrenceDate || r.onceDate || r.nextFireAt);
      return d >= -2 && d <= 14;
    })
    .sort((a, b) => daysUntil(a.nextOccurrenceDate || a.onceDate) - daysUntil(b.nextOccurrenceDate || b.onceDate));

  if (!upcoming.length) return null;
  const lead = upcoming[0];
  const d = daysUntil(lead.nextOccurrenceDate || lead.onceDate || lead.nextFireAt);
  const rel = d === 0 ? '今天' : d === 1 ? '明天' : d > 1 ? `${d} 天后` : '近日';
  const more = upcoming.length > 1 ? ` · 另有 ${upcoming.length - 1} 项` : '';

  const doneBtn = h('button', {
    type: 'button',
    class: 'upcoming-done-btn',
    text: '✓ 记完了',
    'aria-label': `标记已完成：${lead.title}`,
    onClick: async (e) => {
      e.stopPropagation();
      doneBtn.disabled = true;
      try {
        if (typeof api.markReminderDone === 'function') await api.markReminderDone(lead.id);
        else await api.patchReminder(lead.id, { status: 'completed' }, lead.version);
        haptic('memorySeal', { tier });
        onMutated?.();
        showUndoToast(`已收好「${lead.title}」`, async () => {
          await api.patchReminder(lead.id, { status: 'active' }, (lead.version ?? 0) + 1).catch(() => {});
          onMutated?.();
        });
      } catch (err) {
        doneBtn.disabled = false;
        haptic('warning');
        whisper(err?.message || '暂时没能标记完成', 2600);
      }
    },
  });

  return h(
    'div',
    {
      class: ['upcoming-ribbon', `tier-${tier}`],
      role: 'region',
      'aria-label': DATE_SECTION_TITLE[tier] || '近期提醒',
      onClick: () => { haptic('light'); onOpenVellum?.(); },
    },
    h('span', { class: 'mw-dot', 'aria-hidden': 'true' }),
    h(
      'span',
      { class: 'upcoming-copy' },
      h('strong', { text: `${rel} · ${lead.title}` }),
      h('small', { text: `${dualDateLabel(lead)}${more}` }),
    ),
    doneBtn,
  );
}

function primaryLeadDay(raw) {
  if (Array.isArray(raw)) return raw.length ? Number(raw[0]) : 1;
  return raw != null ? Number(raw) : 1;
}

function buildEditableItems(rawFacts, rawReminders) {
  const facts = (rawFacts || []).filter((f) => f && f.status !== 'rejected');
  const reminders = (rawReminders || []).filter((r) => r && r.status !== 'cancelled' && r.status !== 'completed');
  const factById = new Map(facts.map((f) => [f.id, f]));
  const used = new Set();
  const dateLines = [];
  const isPending = (f) => Boolean(f && (f.status === 'pending' || f.needsGlance));

  for (const r of reminders) {
    const linked = r.factId ? factById.get(r.factId) : null;
    if (linked) used.add(linked.id);
    dateLines.push({
      kind: 'reminder',
      raw: r,
      id: r.id,
      version: r.version,
      status: r.status,
      leadDays: primaryLeadDay(r.leadDays),
      when: dualDateLabel(r) || formatFactDateCn(linked),
      text: r.title || linked?.content || '',
      recordId: linked?.recordId || null,
      pending: r.status === 'proposed' || isPending(linked),
      linkedFact: linked ? { id: linked.id, version: linked.version, pending: isPending(linked) } : null,
    });
  }
  for (const f of facts) {
    if (!used.has(f.id) && (f.type === 'date' || f.triggerDate || f.isLunar)) {
      used.add(f.id);
      dateLines.push({
        kind: 'fact',
        id: f.id,
        version: f.version,
        status: f.status,
        when: formatFactDateCn(f),
        text: f.content || '',
        recordId: f.recordId || null,
        pending: isPending(f),
      });
    }
  }
  const detailLines = facts
    .filter((f) => !used.has(f.id))
    .map((f) => ({ kind: 'fact', id: f.id, version: f.version, status: f.status, when: '', text: f.content || '', recordId: f.recordId || null, pending: isPending(f) }));
  return { dateLines, detailLines };
}

function renderVellumRow(item, person, allReminders, { onJumpToRecord, onRemoved, onMutated }) {
  const tier = person.tier || 'crystal';
  const isRem = item.kind === 'reminder' && Boolean(item.id);
  const ctrl = h('span', { class: 'vellum-rem-controls vellum-row-ctrl' });
  const lineBtn = isRem
    ? h(
        'button',
        {
          type: 'button',
          class: ['vellum-line', 'is-reminder', item.pending && 'is-pending', !item.recordId && 'is-static'],
          onClick: () => item.recordId && onJumpToRecord?.(item.recordId),
        },
        h('span', { class: 'vellum-line-text', text: item.text }),
      )
    : h(
        'button',
        {
          type: 'button',
          class: ['vellum-line', item.pending && 'is-pending', !item.recordId && 'is-static'],
          onClick: () => item.recordId && onJumpToRecord?.(item.recordId),
        },
        item.when ? h('span', { class: 'vellum-line-when', text: item.when }) : h('span', { class: 'mw-dot', 'aria-hidden': 'true' }),
        h('span', { class: 'vellum-line-text', text: item.text }),
      );
  const rowEl = isRem
    ? h(
        'div',
        { class: 'vellum-rem-row vellum-row' },
        h('div', { class: 'vellum-rem-head' }, item.when ? h('span', { class: 'vellum-line-when', text: item.when }) : h('span', { class: 'mw-dot', 'aria-hidden': 'true' }), ctrl),
        lineBtn,
      )
    : h('div', { class: 'vellum-rem-row vellum-row' }, h('div', { class: 'vellum-row-main' }, lineBtn, ctrl));

  if (isRem) {
    const syncBtn = (b) => {
      const on = item.status === 'active';
      b.textContent = on ? '提醒中' : '开启提醒';
      b.classList.toggle('is-on', on);
      b.classList.toggle('is-active', on);
      b.setAttribute('aria-pressed', String(on));
    };
    const remBtn = h('button', { type: 'button', class: 'vellum-lead-chip vellum-micro-btn is-rem', 'aria-label': `切换提醒：${item.text}` });
    syncBtn(remBtn);
    remBtn.addEventListener('click', async () => {
      remBtn.disabled = true;
      const next = item.status === 'active' ? 'paused' : 'active';
      try {
        if (next === 'active') await mwNativeUI?.requestNotificationPermission?.().catch(() => null);
        const res = await api.patchReminder(item.id, { status: next }, item.version);
        item.status = res?.status || next;
        if (res?.version != null) item.version = res.version;
        if (item.raw) item.raw.status = item.status;
        syncNativeReminders(person, allReminders);
        haptic('selection');
        syncBtn(remBtn);
        onMutated?.();
        whisper(item.status === 'active' ? '提醒已开启' : '提醒已暂停', 1800);
      } catch (err) {
        haptic('warning');
        whisper(err?.message || '未能更新提醒状态', 2600);
      } finally {
        remBtn.disabled = false;
      }
    });

    const doneBtn = h('button', {
      type: 'button',
      class: 'vellum-done-btn vellum-micro-btn is-done',
      text: '✓ 记完了',
      onClick: async () => {
        doneBtn.disabled = true;
        try {
          if (typeof api.markReminderDone === 'function') await api.markReminderDone(item.id);
          else await api.patchReminder(item.id, { status: 'completed' }, item.version);
          haptic('memorySeal', { tier });
          rowEl.remove();
          onRemoved?.();
          onMutated?.();
          whisper('好，记完了', 2000);
        } catch (err) {
          doneBtn.disabled = false;
          haptic('warning');
          whisper(err?.message || '未能完成提醒', 2600);
        }
      },
    });
    ctrl.append(doneBtn, remBtn);

    const leadRow = h(
      'div',
      { class: 'vellum-rem-controls vellum-lead-row', role: 'group', 'aria-label': '提前提醒天数' },
      ...LEAD_CHOICES.map((days) =>
        h('button', {
          type: 'button',
          class: ['vellum-lead-chip', item.leadDays === days && 'is-active', item.leadDays === days && 'is-selected'],
          text: days === 0 ? '当天' : `提前 ${days} 天`,
          onClick: async () => {
            if (item.leadDays === days) return;
            try {
              const fn = api.updateReminder || api.patchReminder;
              const res = await fn(item.id, { leadDays: [days] }, item.version);
              item.leadDays = primaryLeadDay(res?.leadDays ?? [days]);
              if (res?.version != null) item.version = res.version;
              if (item.raw) item.raw.leadDays = [item.leadDays];
              leadRow.querySelectorAll('.vellum-lead-chip').forEach((c, idx) => {
                const on = LEAD_CHOICES[idx] === item.leadDays;
                c.classList.toggle('is-active', on);
                c.classList.toggle('is-selected', on);
              });
              syncNativeReminders(person, allReminders);
              haptic('selection');
              whisper(days === 0 ? '已改为当天提醒' : `已改为提前 ${days} 天提醒`, 1800);
            } catch (err) {
              haptic('warning');
              whisper(err?.message || '未能修改提前天数', 2400);
            }
          },
        }),
      ),
    );
    rowEl.appendChild(leadRow);
    return rowEl;
  }

  if (item.kind === 'fact' && item.id) {
    if (item.pending) {
      const inkBtn = h('button', {
        type: 'button',
        class: 'vellum-micro-btn is-ink',
        text: '落墨',
        onClick: async () => {
          inkBtn.disabled = true;
          try {
            await api.patchFact(item.id, { status: 'confirmed' }, item.version);
            item.pending = false;
            lineBtn.classList.remove('is-pending');
            inkBtn.remove();
            haptic('memorySeal', { tier });
            onMutated?.();
            whisper('好，已经落墨', 2000);
          } catch (err) {
            inkBtn.disabled = false;
            whisper(err?.message || '未能落墨', 2600);
          }
        },
      });
      ctrl.appendChild(inkBtn);
    } else {
      let armed = false;
      const forgetBtn = h('button', {
        type: 'button',
        class: 'vellum-micro-btn is-forget',
        text: '淡忘',
        onClick: async () => {
          if (!armed) {
            armed = true;
            haptic('warning');
            forgetBtn.textContent = '再点淡忘';
            setTimeout(() => { armed = false; forgetBtn.textContent = '淡忘'; }, 3500);
            return;
          }
          forgetBtn.disabled = true;
          try {
            await api.patchFact(item.id, { status: 'rejected' }, item.version);
            haptic('warning');
            rowEl.remove();
            onRemoved?.();
            onMutated?.();
          } catch (err) {
            forgetBtn.disabled = false;
            whisper(err?.message || '未能淡忘', 2600);
          }
        },
      });
      ctrl.appendChild(forgetBtn);
    }
  }
  return rowEl;
}

export async function openVellumPocket(person, { onJumpToRecord, onMutated } = {}) {
  if (!person) return;
  haptic('light');
  const tier = person.tier || 'crystal';
  const content = h('div', { class: 'vellum-content' }, h('p', { class: 'vellum-loading', text: '正在展开人物档案…' }));
  const editPersonBar = h(
    'div',
    { class: 'vellum-top-actions' },
    h('span', { class: 'journal-tier-tag', text: tier === 'silver' ? '银盾 · 边界' : tier === 'gold' ? '金箭 · 珍重' : '水晶 · 日常' }),
    h('button', {
      type: 'button',
      class: 'vellum-edit-person-btn',
      text: '编辑资料 / 删除',
      onClick: () => {
        haptic('light');
        actions.openPeopleSheet?.({ mode: 'edit', person, stacked: true });
      },
    }),
  );

  openSheet({
    title: person.nameCn,
    sub: person.roleTag || '关于这位在意的人的备忘与日子',
    body: h('div', { class: ['vellum-pocket', `tier-${tier}`] }, editPersonBar, content),
  });

  const [brief, factsRes, remRes] = await Promise.all([
    api.brief(person.id).catch(() => null),
    api.facts(person.id, { limit: 100 }).catch(() => ({ items: [] })),
    api.reminders(person.id).catch(() => ({ items: [] })),
  ]);
  if (!content.isConnected) return;
  clear(content);

  const reminders = remRes?.items || [];
  syncNativeReminders(person, reminders);

  const headline = String(brief?.headline || '').trim();
  if (headline) {
    content.appendChild(
      h(
        'section',
        { class: 'vellum-brief-card', 'aria-label': '近期备忘' },
        h('p', { class: 'vellum-brief-headline' }, h('span', { class: 'mw-dot', 'aria-hidden': 'true' }), h('span', { text: headline })),
        brief?.subline ? h('p', { class: 'vellum-brief-subline', text: brief.subline }) : null,
        h('button', {
          type: 'button',
          class: 'vellum-brief-cta',
          text: BRIEF_CTA[tier] || BRIEF_CTA.gold,
          onClick: () => {
            haptic('light');
            closeSheet({ silentHaptic: true });
            actions.openChat(person.id, { initialPrompt: String(brief?.prompt || headline) });
          },
        }),
      ),
    );
  }

  const { dateLines, detailLines } = buildEditableItems(factsRes?.items, reminders);
  const checkEmpty = () => {
    if (!content.querySelector('.vellum-row') && !headline) {
      content.appendChild(h('p', { class: 'vellum-empty', text: `关于「${person.nameCn}」的日子与细节，会慢慢整理在这里。` }));
    }
  };
  const mount = (title, items) => {
    if (!items.length) return;
    const sec = h('section', { class: 'vellum-section' }, h('h3', { class: 'vellum-sec-title', text: title }));
    for (const item of items) {
      sec.appendChild(renderVellumRow(item, person, reminders, {
        onJumpToRecord,
        onRemoved: () => { if (!sec.querySelector('.vellum-row')) sec.remove(); checkEmpty(); },
        onMutated,
      }));
    }
    content.appendChild(sec);
  };
  mount(DATE_SECTION_TITLE[tier] || '记挂的日子', dateLines);
  mount(tier === 'silver' ? '边界与留痕' : '记住的小事', detailLines);
  checkEmpty();
}
