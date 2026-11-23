// offline-queue.js — local draft queue for captures that could not reach the server.
// Storage is abstracted (IndexedDB in the browser, memory in tests). Audio blobs are
// stored as ArrayBuffer + type for broad Safari compatibility.

import { isRetryableError } from './api/client.js';

export async function toStorable(draft) {
  const rec = { ...draft };
  if (draft.blob && typeof draft.blob.arrayBuffer === 'function') {
    rec.audio = { bytes: await draft.blob.arrayBuffer(), type: draft.blob.type || draft.mimeType || '' };
  }
  delete rec.blob;
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
    async put(rec) {
      map.set(rec.id, clone(rec));
    },
    async getAll() {
      return Array.from(map.values()).map(clone);
    },
    async delete(id) {
      map.delete(id);
    },
  };
}

export function idbStorage(dbName = 'mindweave-drafts', storeName = 'drafts') {
  let dbp = null;
  const open = () => {
    if (!dbp) {
      dbp = new Promise((resolve, reject) => {
        const req = indexedDB.open(dbName, 1);
        req.onupgradeneeded = () => {
          if (!req.result.objectStoreNames.contains(storeName)) req.result.createObjectStore(storeName, { keyPath: 'id' });
        };
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
      });
    }
    return dbp;
  };
  const tx = async (mode, fn) => {
    const db = await open();
    return new Promise((resolve, reject) => {
      const t = db.transaction(storeName, mode);
      const store = t.objectStore(storeName);
      const req = fn(store);
      t.oncomplete = () => resolve(req ? req.result : undefined);
      t.onerror = () => reject(t.error);
      t.onabort = () => reject(t.error);
    });
  };
  return {
    put: (rec) => tx('readwrite', (s) => s.put(rec)),
    getAll: () => tx('readonly', (s) => s.getAll()),
    delete: (id) => tx('readwrite', (s) => s.delete(id)),
  };
}

/**
 * @param {{put, getAll, delete}} storage
 * @param {{send: (draft) => Promise<any>}} opts  send() must reuse draft.idemKey
 */
export function createDraftQueue(storage, { send }) {
  let flushing = null;

  async function add(draft) {
    if (!draft || !draft.id || !draft.idemKey) throw new Error('draft needs id and idemKey');
    const rec = await toStorable({
      kind: draft.blob ? 'voice' : 'text',
      createdAt: new Date().toISOString(),
      attempts: 0,
      ...draft,
    });
    await storage.put(rec);
    return rec;
  }

  async function list() {
    const all = await storage.getAll();
    return all.sort((a, b) => String(a.createdAt).localeCompare(String(b.createdAt))).map(fromStorable);
  }

  async function count() {
    return (await storage.getAll()).length;
  }

  /** Send drafts oldest-first. Stops at the first retryable failure; drops permanent failures. */
  function flush() {
    if (flushing) return flushing;
    flushing = (async () => {
      const sent = [];
      const dropped = [];
      let stopped = false;
      for (const draft of await list()) {
        try {
          const result = await send(draft);
          await storage.delete(draft.id);
          sent.push({ draft, result });
        } catch (err) {
          if (isRetryableError(err)) {
            stopped = true;
            break;
          }
          await storage.delete(draft.id);
          dropped.push({ draft, error: err });
        }
      }
      return { sent, dropped, stopped };
    })().finally(() => {
      flushing = null;
    });
    return flushing;
  }

  return { add, list, count, flush };
}
