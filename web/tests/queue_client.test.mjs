import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createDraftQueue, memoryStorage, toStorable, fromStorable } from '../scripts/offline-queue.js';
import { ApiError, createClient } from '../scripts/api/client.js';

test('drafts serialize blobs to ArrayBuffer and back losslessly', async () => {
  const blob = new Blob([new Uint8Array([1, 2, 3, 250])], { type: 'audio/webm' });
  const stored = await toStorable({ id: 'd1', idemKey: 'k1', blob, routeMode: 'current', activePersonId: 'p1' });
  assert.equal(stored.blob, undefined);
  assert.ok(stored.audio.bytes instanceof ArrayBuffer);
  const clone = structuredClone(stored);
  const back = fromStorable(clone);
  assert.equal(back.blob.type, 'audio/webm');
  assert.deepEqual([...new Uint8Array(await back.blob.arrayBuffer())], [1, 2, 3, 250]);
  assert.equal(back.idemKey, 'k1');
});

test('flush re-sends with the same Idempotency-Key, oldest first, then empties the queue', async () => {
  const sentKeys = [];
  const q = createDraftQueue(memoryStorage(), { send: async (d) => (sentKeys.push(d.idemKey), { ok: true }) });
  await q.add({ id: 'a', idemKey: 'key-a', text: '她不吃香菜', routeMode: 'current', activePersonId: 'p', createdAt: '2026-01-01T00:00:00Z' });
  await q.add({ id: 'b', idemKey: 'key-b', blob: new Blob(['x']), routeMode: 'global', createdAt: '2026-01-02T00:00:00Z' });
  assert.equal(await q.count(), 2);
  const res = await q.flush();
  assert.deepEqual(sentKeys, ['key-a', 'key-b']);
  assert.equal(res.sent.length, 2);
  assert.equal(await q.count(), 0);
});

test('network failure stops the flush and keeps drafts for later', async () => {
  let calls = 0;
  const q = createDraftQueue(memoryStorage(), {
    send: async () => {
      calls++;
      throw new ApiError({ code: 'network', status: 0 });
    },
  });
  await q.add({ id: 'a', idemKey: 'k', text: 'x', createdAt: '1' });
  await q.add({ id: 'b', idemKey: 'k2', text: 'y', createdAt: '2' });
  const res = await q.flush();
  assert.equal(res.stopped, true);
  assert.equal(calls, 1);
  assert.equal(await q.count(), 2);
});

test('permanent 4xx failures are dropped instead of retried forever', async () => {
  const q = createDraftQueue(memoryStorage(), {
    send: async () => {
      throw new ApiError({ code: 'validation_failed', status: 422 });
    },
  });
  await q.add({ id: 'a', idemKey: 'k', text: 'x' });
  const res = await q.flush();
  assert.equal(res.dropped.length, 1);
  assert.equal(await q.count(), 0);
});

test('drafts require id + idemKey', async () => {
  const q = createDraftQueue(memoryStorage(), { send: async () => ({}) });
  await assert.rejects(() => q.add({ id: 'a' }));
});

test('client: refresh on 401 retries once with the same Idempotency-Key; errors are friendly Chinese', async () => {
  const calls = [];
  const store = new Map([['mw.auth', JSON.stringify({ accessToken: 'old', refreshToken: 'r1' })]]);
  const storage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v), removeItem: (k) => store.delete(k) };
  const json = (status, body) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
  const fetchImpl = async (url, init) => {
    calls.push({ url, auth: init.headers.Authorization, idem: init.headers['Idempotency-Key'] });
    if (url.endsWith('/auth/refresh')) return json(200, { accessToken: 'new', refreshToken: 'r2', expiresIn: 900 });
    if (init.headers.Authorization === 'Bearer old') return json(401, { code: 'unauthorized', status: 401 });
    if (url.includes('/fail')) return json(422, { code: 'validation_failed', status: 422, detail: 'field x invalid' });
    return json(201, { captureId: 'c1' });
  };
  const client = createClient({ fetchImpl, storage, base: '/v1' });
  const res = await client.post('/captures', { text: 'x' });
  assert.equal(res.captureId, 'c1');
  const captureCalls = calls.filter((c) => c.url === '/v1/captures');
  assert.equal(captureCalls.length, 2);
  assert.equal(captureCalls[0].idem, captureCalls[1].idem);
  assert.equal(captureCalls[1].auth, 'Bearer new');
  await assert.rejects(client.post('/fail', {}), (err) => {
    assert.equal(err.code, 'validation_failed');
    assert.ok(!err.message.includes('field x'));
    assert.match(err.message, /[\u4e00-\u9fa5]/);
    return true;
  });
  const offline = createClient({ fetchImpl: async () => { throw new TypeError('Failed to fetch'); }, storage, base: '/v1' });
  await assert.rejects(offline.get('/bootstrap'), (err) => err.code === 'network' && err.retryable);
});
