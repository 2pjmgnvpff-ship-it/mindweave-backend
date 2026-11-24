import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  groupMaterialsByMonth,
  highlightParts,
  matchesMaterialQuery,
  buildVellumSections,
  formatLunarCn,
  sanitizeAgentStep,
  summarizeAgentTrace,
  formatActionPreview,
} from '../scripts/format.js';

test('groupMaterialsByMonth groups descending and labels cross-year months cleanly', () => {
  const chapters = groupMaterialsByMonth([
    { id: '1', happenedOn: '2025-12-21', body: '浅驼色羊绒围巾' },
    { id: '2', happenedOn: '2026-11-16', body: '膝盖复查' },
    { id: '3', happenedOn: '2026-11-08', body: '桂花乌龙' },
    { id: '4', body: '无日期' },
  ]);
  assert.deepEqual(chapters.map((c) => c.ym), ['2026-11', '2025-12', 'earlier']);
  assert.equal(chapters[0].shortLabel, '11月');
  assert.equal(chapters[0].items.length, 2);
  assert.equal(chapters[1].shortLabel, '12月');
  assert.equal(chapters[2].label, '更早');
});

test('highlightParts splits safely without HTML injection', () => {
  const parts = highlightParts('冬至买一条浅驼色羊绒围巾，围巾真暖和', '围巾');
  assert.deepEqual(parts, [
    { text: '冬至买一条浅驼色羊绒', hit: false },
    { text: '围巾', hit: true },
    { text: '，', hit: false },
    { text: '围巾', hit: true },
    { text: '真暖和', hit: false },
  ]);
  assert.deepEqual(highlightParts('<b>围巾</b>', '围巾')[1], { text: '围巾', hit: true });
});

test('matchesMaterialQuery searches body, snippet, and Chinese date', () => {
  const item = { body: '喜欢桂花乌龙', snippet: '桂花乌龙', happenedOn: '2026-11-08' };
  assert.equal(matchesMaterialQuery(item, '乌龙'), true);
  assert.equal(matchesMaterialQuery(item, '11月8日'), true);
  assert.equal(matchesMaterialQuery(item, '花生'), false);
});

test('buildVellumSections deduplicates reminder-linked facts and preserves lunar formatting', () => {
  assert.equal(formatLunarCn(5, 12), '农历五月十二');
  assert.equal(formatLunarCn(8, 15, true), '农历闰八月十五');
  const { dateLines, detailLines } = buildVellumSections(
    [
      { id: 'f1', type: 'date', status: 'pending', content: '农历五月十二生日', isLunar: true, lunarMonth: 5, lunarDay: 12, recordId: 'r1' },
      { id: 'f2', type: 'taboo', status: 'confirmed', content: '对香菜过敏', recordId: 'r2' },
      { id: 'f3', type: 'taboo', status: 'rejected', content: '已撤回的记录', recordId: 'r3' },
    ],
    [
      { id: 'rem1', factId: 'f1', status: 'proposed', title: '妈妈生日提醒', isLunar: true, targetMonth: 5, targetDay: 12 },
    ],
  );
  assert.equal(dateLines.length, 1);
  assert.equal(dateLines[0].when, '农历五月十二');
  assert.equal(dateLines[0].recordId, 'r1');
  assert.equal(dateLines[0].pending, true);
  assert.equal(detailLines.length, 1);
  assert.equal(detailLines[0].text, '对香菜过敏');
});

test('sanitizeAgentStep strips UUIDs and normalizes invalid payloads', () => {
  const cleaned = sanitizeAgentStep({
    step: 1,
    phase: 'SCAN',
    label: '翻阅「妈妈」 550e8400-e29b-41d4-a716-446655440000 的 18 页手帐…',
    detail: 'id 12345678-1234-1234-1234-1234567890ab 已核验',
    count: 18,
  });
  assert.equal(cleaned.phase, 'scan');
  assert.equal(cleaned.label, '翻阅「妈妈」 的 18 页手帐…');
  assert.equal(cleaned.detail, 'id 已核验');
  assert.equal(cleaned.count, 18);
  assert.equal(sanitizeAgentStep(null), null);
  assert.equal(sanitizeAgentStep({ label: '   ' }), null);
});

test('summarizeAgentTrace builds calm Chinese summary across scan, memory, and recall steps', () => {
  const full = summarizeAgentTrace([
    { step: 1, phase: 'scan', label: '翻阅「妈妈」的 18 页手帐…', count: 18 },
    { step: 2, phase: 'memory', label: '核对 5 条已记住的细节与 2 项日程…', count: 7 },
    { step: 3, phase: 'recall', label: '找到 4 处相关原话', count: 4 },
  ]);
  assert.ok(full.includes('18'));
  assert.equal(summarizeAgentTrace([{ step: 1, label: '正在整理边界提醒…' }]), '正在整理边界提醒');
  assert.equal(summarizeAgentTrace([]), '');
});

test('formatActionPreview converts bracketed ISO dates to Chinese dates and collapses raw logs', () => {
  const preview = formatActionPreview('[2026-11-12] 会议临时推迟\n- [2026-11-18] 未经商量改动预算');
  assert.equal(preview, '11月12日 会议临时推迟 · 11月18日 未经商量改动预算');
  assert.ok(!preview.includes('[2026-'));
});

test('V2 chat-render & crisisCard: borderless ai-para, zero sparkle/generic pronoun, BC6 onceDate & persisted suggestions, offline 12356', async () => {
  const { installFakeDom } = await import('./helpers/fake-dom.mjs');
  const doc = installFakeDom();
  doc.querySelector = () => null;
  const { api } = await import('../scripts/api/endpoints.js');
  const { store } = await import('../scripts/store.js');
  const { agentTracePill, perspectiveCard, actionProposalCard, citePill, historyMessage, starterChips, crisisCard } = await import('../scripts/views/chat-render.js');

  store.set({
    people: {
      'person-1': { id: 'person-1', nameCn: '妈妈', tier: 'gold' },
      'person-silver': { id: 'person-silver', nameCn: '陈经理', tier: 'silver' },
    },
  });

  // Trace pill uses pure CSS dot (zero sparkle glyph)
  const trace = agentTracePill();
  assert.equal(trace.el.hidden, true);
  trace.addStep({ step: 1, phase: 'scan', label: '翻阅「妈妈」的 18 页手帐…', count: 18 });
  assert.equal(trace.el.hidden, false);
  trace.settle();
  assert.ok(!trace.el.textContent.includes('\u2726'));

  // Silver and Crystal NEVER render perspectiveCard
  assert.equal(perspectiveCard(['换个角度'], 'silver'), null);
  assert.equal(perspectiveCard(['换个角度'], 'crystal'), null);
  assert.ok(perspectiveCard(['换个角度'], 'gold'));

  // Persisted historyMessage renders suggestions (BC3 BUG-CC-10) & respects Gold-only perspectives (BC6)
  const goldMsgs = historyMessage(
    { role: 'assistant', content: '第一段\n\n第二段', perspectives: ['换个角度看'], suggestions: ['妈，周日我陪你去'] },
    'person-1',
    'gold',
  );
  assert.equal(goldMsgs.length, 3);
  const silverMsgs = historyMessage(
    { role: 'assistant', content: '客观时间线', perspectives: ['不应显示'], suggestions: ['请邮件确认排期'] },
    'person-silver',
    'silver',
  );
  assert.equal(silverMsgs.length, 2); // bubble + suggestions, NO perspectiveCard!

  // Personalized Gold starters (zero generic pronoun)
  const starters = starterChips('gold', () => {}, '妈妈');
  assert.ok(starters[0].textContent.includes('妈妈'));

  // Cite pill uses monochrome SVG & readable excerpt
  const cite = citePill(
    { materialId: 'm1', snippet: '妈妈最近总是说左膝盖阴雨天会隐隐作痛', happenedOn: '2026-11-18', type: 'note' },
    'person-1',
  );
  assert.ok(cite.outerHTML.includes('<svg'));
  assert.ok(cite.textContent.includes('妈妈最近总是说左膝盖阴雨天会…'));

  // Offline Crisis Sanctuary Card ALWAYS renders China NHC 12356 + 010-82951332
  const crisis = crisisCard({});
  assert.ok(crisis.outerHTML.includes('tel:12356'));
  assert.ok(crisis.outerHTML.includes('tel:01082951332'));
  assert.ok(crisis.textContent.includes('此刻你不必独自扛着'));

  // Action proposal card sends BC6 onceDate on reminder & compact Gleb done label
  const origReminder = api.createReminder;
  const origMaterial = api.createMaterial;
  let remPayload = null;
  let createdMat = null;
  try {
    api.createReminder = async (pid, payload) => {
      remPayload = { pid, payload };
      return { id: 'rem-ok' };
    };
    api.createMaterial = async (pid, payload) => {
      createdMat = { pid, payload };
      return { id: 'mat-ok' };
    };
    const remCard = actionProposalCard(
      { id: 'a1', kind: 'create_reminder', label: '设好提醒', title: '<img onerror=alert(1)> 买围巾', triggerDate: '2026-12-21', whenLabel: '12月21日' },
      'person-1',
    );
    assert.ok(remCard);
    assert.equal(remCard.rawHtml, null);
    assert.ok(remCard.outerHTML.includes('&lt;img onerror=alert(1)&gt;'));
    const remBtn = remCard.children.find((c) => c.tagName === 'BUTTON');
    await remBtn.listeners.click[0]();
    assert.equal(remBtn.textContent, '已设好');
    assert.equal(remPayload.payload.onceDate, '2026-12-21');
    assert.equal(remPayload.payload.triggerDate, undefined);

    const noteCard = actionProposalCard(
      { id: 'a2', kind: 'save_note', label: '存为书面留痕', title: '客观时间线要点', body: '[2026-11-12] 第一次延期' },
      'person-silver',
    );
    const noteBtn = noteCard.children.find((c) => c.tagName === 'BUTTON');
    await noteBtn.listeners.click[0]();
    assert.equal(noteBtn.textContent, '已留档');
    assert.equal(createdMat.pid, 'person-silver');
  } finally {
    api.createReminder = origReminder;
    api.createMaterial = origMaterial;
  }
});
