import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  groupMaterialsByMonth,
  highlightParts,
  matchesMaterialQuery,
  buildVellumSections,
  formatLunarCn,
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
