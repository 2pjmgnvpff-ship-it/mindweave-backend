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

test('summarizeAgentTrace builds poetic Chinese summary across scan, memory, and recall steps', () => {
  const full = summarizeAgentTrace([
    { step: 1, phase: 'scan', label: '翻阅「妈妈」的 18 页手帐…', count: 18 },
    { step: 2, phase: 'memory', label: '核对 5 条已记住的细节与 2 项日程…', count: 7 },
    { step: 3, phase: 'recall', label: '命中 4 处相关原话', count: 4 },
  ]);
  assert.equal(full, '已查阅 18 页手帐 · 命中 4 处原话');

  const labelOnly = summarizeAgentTrace([
    { step: 1, label: '翻阅「爸爸」的 6 页手帐…' },
    { step: 2, label: '命中 2 处相关原话' },
  ]);
  assert.equal(labelOnly, '已查阅 6 页手帐 · 命中 2 处原话');

  const zeroHit = summarizeAgentTrace([
    { step: 1, phase: 'scan', label: '翻阅「同事」的 9 页手帐…', count: 9 },
    { step: 2, phase: 'facts', label: '核对 3 条已记住的细节…', count: 3 },
    { step: 3, phase: 'recall', label: '未命中直接原话', count: 0 },
  ]);
  assert.equal(zeroHit, '已查阅 9 页手帐 · 核对 3 项记忆');

  assert.equal(summarizeAgentTrace([{ step: 1, label: '正在整理边界提醒…' }]), '正在整理边界提醒');
  assert.equal(summarizeAgentTrace([]), '');
});

test('formatActionPreview converts bracketed ISO dates to Chinese dates and collapses raw logs', () => {
  const preview = formatActionPreview('[2026-11-12] 会议临时推迟\n- [2026-11-18] 未经商量改动预算');
  assert.equal(preview, '11月12日 会议临时推迟 · 11月18日 未经商量改动预算');
  assert.ok(!preview.includes('[2026-'));
});

test('agentTracePill, citePill, proactiveBriefCard, and actionProposalCard render monochrome SVGs, compact done text, and zero XSS', async () => {
  const { installFakeDom } = await import('./helpers/fake-dom.mjs');
  const doc = installFakeDom();
  doc.querySelector = () => null;
  const { api } = await import('../scripts/api/endpoints.js');
  const { agentTracePill, perspectiveCard, proactiveBriefCard, actionProposalCard, citePill } = await import('../scripts/views/chat-render.js');

  const trace = agentTracePill();
  assert.equal(trace.el.hidden, true);
  trace.addStep({ step: 1, phase: 'scan', label: '翻阅「妈妈」的 18 页手帐…', count: 18 });
  assert.equal(trace.el.hidden, false);
  assert.ok(trace.el.textContent.includes('翻阅「妈妈」的 18 页手帐…'));
  trace.addStep({ step: 2, phase: 'recall', label: '命中 4 处相关原话', count: 4 });
  trace.settle();
  const details = trace.el.children.find((c) => c.tagName === 'DETAILS');
  assert.ok(details);
  assert.equal(details.hidden, false);
  assert.ok(details.textContent.includes('已查阅 18 页手帐 · 命中 4 处原话'));
  assert.equal(details.rawHtml, null);

  // Silver and Crystal NEVER render perspectiveCard
  assert.equal(perspectiveCard(['换个角度'], 'silver'), null);
  assert.equal(perspectiveCard(['换个角度'], 'crystal'), null);
  assert.ok(perspectiveCard(['换个角度'], 'gold'));

  // Cite pill uses readable <= 14-char excerpt + monochrome SVG (zero raw emoji)
  const cite = citePill(
    { materialId: 'm1', snippet: '妈妈最近总是说左膝盖阴雨天会隐隐作痛', happenedOn: '2026-11-18', type: 'note' },
    'person-1',
  );
  assert.ok(cite.outerHTML.includes('<svg'));
  assert.ok(!cite.textContent.includes('✎'));
  assert.ok(!cite.textContent.includes('♪'));
  assert.ok(cite.textContent.includes('妈妈最近总是说左膝盖阴雨天会…'));

  // Proactive brief card wires 1-tap CTA cleanly
  let asked = null;
  const brief = proactiveBriefCard(
    { headline: '周日是妈妈生日', subline: '手帐里提过她喜欢桂花乌龙', prompt: '结合手帐帮我准备妈妈生日安排' },
    'gold',
    (p) => (asked = p),
  );
  assert.ok(brief);
  const cta = brief.children.find((c) => c.tagName === 'BUTTON');
  cta.listeners.click[0]();
  assert.equal(asked, '结合手帐帮我准备妈妈生日安排');

  // Action proposal card: reminder uses monochrome BELL_SVG (zero ⏰), XSS safe, compact done label
  const origReminder = api.createReminder;
  const origMaterial = api.createMaterial;
  let createdMat = null;
  try {
    api.createReminder = async () => ({ id: 'rem-ok' });
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
    assert.ok(remCard.outerHTML.includes('<svg'));
    assert.ok(!remCard.textContent.includes('⏰'));
    assert.ok(remCard.outerHTML.includes('&lt;img onerror=alert(1)&gt;'));
    const remBtn = remCard.children.find((c) => c.tagName === 'BUTTON');
    await remBtn.listeners.click[0]();
    assert.equal(remBtn.textContent, '✓ 已设好提醒');

    const noteCard = actionProposalCard(
      { id: 'a2', kind: 'save_note', label: '存为书面留痕', title: '客观时间线要点', body: '[2026-11-12] 第一次延期\n[2026-11-18] 第二次变更' },
      'person-silver',
    );
    assert.ok(noteCard);
    assert.ok(noteCard.outerHTML.includes('<svg'));
    assert.ok(!noteCard.textContent.includes('✎'));
    const noteBtn = noteCard.children.find((c) => c.tagName === 'BUTTON');
    await noteBtn.listeners.click[0]();
    assert.equal(noteBtn.textContent, '✓ 已存入手帐');
    assert.equal(createdMat.pid, 'person-silver');
    assert.equal(createdMat.payload.kind, 'note');
    assert.equal(createdMat.payload.title, '客观时间线要点');
  } finally {
    api.createReminder = origReminder;
    api.createMaterial = origMaterial;
  }
});
