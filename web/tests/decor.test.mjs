import { test } from 'node:test';
import assert from 'node:assert/strict';
import { decorFor, materialDecor, hash32, seededRandom, safeColor, personDecor, monogramOf, PALETTE } from '../scripts/decor.js';

test('same input → identical decoration', () => {
  const a = decorFor({ id: 'p-1', date: '2026-04-17', tier: 'gold' });
  const b = decorFor({ id: 'p-1', date: '2026-04-17', tier: 'gold' });
  assert.deepEqual(a, b);
});

test('date accepts ISO timestamps and YYYY-MM-DD equivalently', () => {
  assert.deepEqual(decorFor({ id: 'x', date: '2026-04-17T09:00:00+08:00', tier: 'silver' }), decorFor({ id: 'x', date: '2026-04-17', tier: 'silver' }));
});

test('different ids/tiers change the decoration', () => {
  const a = decorFor({ id: 'p-1', date: '2026-04-17', tier: 'gold' });
  const b = decorFor({ id: 'p-2', date: '2026-04-17', tier: 'gold' });
  const c = decorFor({ id: 'p-1', date: '2026-04-17', tier: 'crystal' });
  assert.notDeepEqual(a, b);
  assert.notDeepEqual(a, c);
});

test('derived strings follow the expected Chinese/stamp format', () => {
  const d = decorFor({ id: 'p-1', date: '2026-04-17', tier: 'gold' });
  assert.match(d.ticketNo, /^№ 0417-[A-K]$/);
  assert.equal(d.stampCode, '心络 · 04.17');
  assert.equal(d.folioDate, '04.17');
  assert.equal(d.hoboDay, '17');
  assert.equal(d.hoboMonthWk, '4月 · 周五');
  assert.match(d.filmCaption, /^胶片 · 第 \d+ 格$/);
  assert.ok(Math.abs(d.rotation) <= 2.5);
});

test('colours come only from the palette', () => {
  for (const tier of ['gold', 'silver', 'crystal']) {
    for (let i = 0; i < 30; i++) {
      const d = decorFor({ id: `id-${i}`, date: '2026-01-0' + ((i % 9) + 1), tier });
      assert.equal(d.rotation, 0);
      assert.ok(PALETTE.tab[tier].includes(d.tabColor));
      assert.ok(PALETTE.seal[tier].some(([bg, fg]) => bg === d.sealBg && fg === d.sealColor));
    }
  }
});

test('hash/prng are stable', () => {
  assert.equal(hash32('mindweave'), hash32('mindweave'));
  const r1 = seededRandom(42);
  const r2 = seededRandom(42);
  for (let i = 0; i < 5; i++) assert.equal(r1(), r2());
});

test('materialDecor is deterministic per material', () => {
  assert.deepEqual(materialDecor({ id: 'm1' }, 'gold'), materialDecor({ id: 'm1' }, 'gold'));
});

test('safeColor rejects CSS injection and keeps plain colours', () => {
  assert.equal(safeColor('#fff', 'x'), '#fff');
  assert.equal(safeColor('rgba(1, 2, 3, 0.5)', 'x'), 'rgba(1, 2, 3, 0.5)');
  assert.equal(safeColor('red; background:url(http://evil)', 'x'), 'x');
  assert.equal(safeColor('url(javascript:alert(1))', 'x'), 'x');
  const pd = personDecor({ id: 'p', tier: 'gold', sealBg: 'expression(alert(1))' }, '2026-04-17');
  assert.ok(PALETTE.seal.gold.some(([bg]) => bg === pd.sealBg));
});

test('monogram falls back to first character of the name', () => {
  assert.equal(monogramOf({ nameCn: '妈妈' }), '妈');
  assert.equal(monogramOf({ monogram: '林', nameCn: '林晚' }), '林');
  assert.equal(monogramOf({}), '·');
});
