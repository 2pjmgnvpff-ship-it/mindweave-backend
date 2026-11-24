// offline-queue.js — IDB v2 offline write-ahead draft queue with Safari UnknownError auto-heal,
// inFlightIds lease guard, 401 pause-until-relogin, and quarantined zero-silent-delete (<= 240L).

import { isRetryableError } from './api/client.js';

const IDB_VERSION = 2;
const SAFARI_HEAL_DELAY_MS = 120;

export async function toStorable(draft) {
  const rec = { ...draft };
  if (draft.blob && typeof draft.blob.arrayBuffer === 'function') {
    rec.audio = { bytes: await draft.blob.arrayBuffer(), type: draft.blob.type || draft.mimeType || '' };
  }
  delete rec.blob;
  if (draft.clientTranscript != null) rec.clientTranscript = String(draft.clientTranscript);
  if (draft.speechEngine != null) rec.speechEngine = String(draft.speechEngine);
  if (draft.audioDurationMs != null && Number.isFinite(Number(draft.audioDurationMs))) {
    rec.audioDurationMs = Math.round(Number(draft.audioDurationMs));
  }
  if (!rec.status) rec.status = 'queued';
  return rec;
}

export function fromStorable(rec) {
  const draft = { ...rec };
  if (rec.audio && rec.audio.bytes) {
    draft.blob = new Blob([rec.audio.bytes], { type: rec.audio.type || rec.mimeType || '' });
  }
  delete draft.audio;
  return draft;
}

export function memoryStorage() {
  const map = new Map();
  const clone = (v) => (typeof structuredClone === 'function' ? structuredClone(v) : JSON.parse(JSON.stringify(v)));
  return {
    async put(rec) { map.set(rec.id, clone(rec)); },
    async getAll() { return Array.from(map.values()).map(clone); },
    async delete(id) { map.delete(id); },
  };
}

function isSafariIdbCorruptErr(err) {
  const name = String(err?.name || '');
  const msg = String(err?.message || '');
  return name === 'UnknownError' || name === 'InvalidStateError' || /connection is closing|backing store/i.test(msg);
}

export function idbStorage(dbName = 'mindweave-drafts', storeName = 'drafts') {
  let dbp = null;
  const resetDb = () => {
    if (dbp) {
      dbp.then((db) => { try { db.close(); } catch { /* ignore */ } }).catch(() => {});
      dbp = null;
    }
  };
  const open = () => {
    if (!dbp) {
      dbp = new Promise((resolve, reject) => {
        const req = indexedDB.open(dbName, IDB_VERSION);
        req.onupgradeneeded = () => {
          const db = req.result;
          const store = db.objectStoreNames.contains(storeName)
            ? req.transaction.objectStore(storeName)
            : db.createObjectStore(storeName, { keyPath: 'id' });
          if (!store.indexNames.contains('by_status')) store.createIndex('by_status', 'status', { unique: false });
        };
        req.onsuccess = () => {
          const db = req.result;
          db.onversionchange = () => resetDb();
          db.onclose = () => { dbp = null; };
          resolve(db);
        };
        req.onerror = () => { dbp = null; reject(req.error); };
      });
    }
    return dbp;
  };
  const runTx = async (mode, fn) => {
    const db = await open();
    return new Promise((resolve, reject) => {
      const t = db.transaction(storeName, mode);
      const req = fn(t.objectStore(storeName));
      t.oncomplete = () => resolve(req ? req.result : undefined);
      t.onerror = () => reject(t.error);
      t.onabort = () => reject(t.error);
    });
  };
  const tx = async (mode, fn) => {
    try { return await runTx(mode, fn); } catch (err) {
      if (!isSafariIdbCorruptErr(err)) throw err;
      resetDb();
      await new Promise((r) => setTimeout(r, SAFARI_HEAL_DELAY_MS));
      return runTx(mode, fn);
    }
  };
  return {
    put: (rec) => tx('readwrite', (s) => s.put(rec)),
    getAll: () => tx('readonly', (s) => s.getAll()),
    delete: (id) => tx('readwrite', (s) => s.delete(id)),
  };
}

export function createDraftQueue(storage, { send }) {
  let flushing = null;
  let pausedForAuth = false;
  const inFlightIds = new Set();

  async function add(draft) {
    if (!draft || !draft.id || !draft.idemKey) throw new Error('draft needs id and idemKey');
    const rec = await toStorable({
      kind: draft.blob ? 'voice' : 'text',
      createdAt: new Date().toISOString(),
      attempts: 0,
      status: 'queued',
      ...draft,
    });
    await storage.put(rec);
    return rec;
  }

  async function listAllRaw() {
    const all = await storage.getAll();
    return all.sort((a, b) => String(a.createdAt).localeCompare(String(b.createdAt)));
  }

  async function list() { return (await listAllRaw()).filter((r) => r.status !== 'quarantined').map(fromStorable); }
  async function listQuarantined() { return (await listAllRaw()).filter((r) => r.status === 'quarantined').map(fromStorable); }
  async function listAll() { return (await listAllRaw()).map(fromStorable); }
  async function count() { return (await storage.getAll()).filter((r) => r.status !== 'quarantined').length; }
  async function quarantinedCount() { return (await storage.getAll()).filter((r) => r.status === 'quarantined').length; }

  async function retryDraft(id) {
    const rec = (await listAllRaw()).find((r) => r.id === id);
    if (!rec) return false;
    await storage.put({ ...rec, status: 'queued', lastError: null, attempts: 0 });
    pausedForAuth = false;
    return true;
  }

  async function discardDraft(id) {
    inFlightIds.delete(id);
    await storage.delete(id);
  }

  function pauseForAuth() { pausedForAuth = true; }
  function resumeAuth() { pausedForAuth = false; return flush(); }

  function flush() {
    if (pausedForAuth) return Promise.resolve({ sent: [], dropped: [], stopped: true, pausedForAuth: true });
    if (flushing) return flushing;
    flushing = (async () => {
      const sent = [];
      const dropped = [];
      let stopped = false;
      const rawQueued = (await listAllRaw()).filter((r) => r.status !== 'quarantined');
      for (const raw of rawQueued) {
        if (inFlightIds.has(raw.id)) continue;
        const draft = fromStorable(raw);
        inFlightIds.add(raw.id);
        try {
          const result = await send(draft);
          await storage.delete(raw.id);
          sent.push({ draft, result });
        } catch (err) {
          if (err && err.status === 401) { pausedForAuth = true; stopped = true; break; }
          if (isRetryableError(err)) {
            await storage.put({ ...raw, attempts: (raw.attempts || 0) + 1, lastError: err?.message || null });
            stopped = true;
            break;
          }
          await storage.put({
            ...raw,
            status: 'quarantined',
            attempts: (raw.attempts || 0) + 1,
            lastError: err?.message || '发送未能完成',
            quarantinedAt: new Date().toISOString(),
          });
          dropped.push({ draft, error: err });
        } finally {
          inFlightIds.delete(raw.id);
        }
      }
      return { sent, dropped, stopped, pausedForAuth };
    })().finally(() => { flushing = null; });
    return flushing;
  }

  return {
    add,
    list,
    listAll,
    listQuarantined,
    count,
    quarantinedCount,
    flush,
    retryDraft,
    retryQuarantined: retryDraft,
    discardDraft,
    remove: discardDraft,
    pauseForAuth,
    resumeAuth,
    resumePaused: resumeAuth,
    isPausedForAuth: () => pausedForAuth,
  };
}
