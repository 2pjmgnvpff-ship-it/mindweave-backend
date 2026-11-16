// store.js — single app state store with subscribe. No global mutable people DB:
// everything comes from /v1/bootstrap and API responses.

export function createStore(initial) {
  let state = initial;
  const subs = new Set();
  return {
    get: () => state,
    set(patch) {
      const next = typeof patch === 'function' ? patch(state) : patch;
      state = { ...state, ...next };
      for (const fn of subs) fn(state);
    },
    subscribe(fn) {
      subs.add(fn);
      return () => subs.delete(fn);
    },
  };
}

export const EDITIONS = ['cotton', 'dotgrid', 'parchment', 'traveler', 'hobonichi', 'museglass'];

export const store = createStore({
  ready: false,
  people: {},
  coreIds: [],
  casualIds: [],
  view: 'journal', // journal | directory
  activePersonId: null,
  lastDemotedId: null,
  edition: 'cotton',
  preferences: null,
  inboxCount: 0,
  questionCount: 0,
  materials: {}, // personId → { items, nextCursor, hasMore, loading, loaded }
  pending: [], // optimistic slips: { id, personId|null, text, state }
  draftCount: 0,
});

export function applyBootstrap(data) {
  const people = { ...(data?.people || {}) };
  store.set({
    ready: true,
    people,
    coreIds: (data?.coreIds || []).filter((id) => people[id]),
    casualIds: (data?.casualIds || []).filter((id) => people[id]),
    preferences: data?.preferences || null,
    inboxCount: data?.inboxCount || 0,
    questionCount: data?.questionCount || 0,
  });
}

export function upsertPerson(person) {
  if (!person?.id) return;
  const s = store.get();
  const prev = s.people[person.id] || {};
  store.set({ people: { ...s.people, [person.id]: { ...prev, ...person } } });
}

/** Apply a PATCH /tier response (or a local optimistic change). */
export function applyTierChange({ personId, tier, coreIds, casualIds }) {
  const s = store.get();
  const p = s.people[personId];
  let people = s.people;
  if (p) {
    const next = { ...p, tier };
    if (p.tier !== tier) {
      delete next.sealBg;
      delete next.sealColor;
      delete next.tapeColor;
      delete next.tabColor;
    }
    people = { ...s.people, [personId]: next };
  }
  let core = coreIds;
  let casual = casualIds;
  if (!core || !casual) {
    core = s.coreIds.filter((id) => id !== personId);
    casual = s.casualIds.filter((id) => id !== personId);
    if (tier === 'crystal') casual = [personId, ...casual];
    else core = [...core, personId];
  }
  store.set({ people, coreIds: core, casualIds: casual });
}

export function setMaterials(personId, page, { append = false } = {}) {
  const s = store.get();
  const prev = s.materials[personId];
  const items = append && prev ? dedupe([...prev.items, ...(page.items || [])]) : page.items || [];
  store.set({
    materials: {
      ...s.materials,
      [personId]: { items, nextCursor: page.nextCursor || null, hasMore: Boolean(page.hasMore), loaded: true, loading: false },
    },
  });
}

export function patchMaterial(personId, materialId, patch) {
  const s = store.get();
  const prev = s.materials[personId];
  if (!prev) return;
  const items = prev.items.map((m) => (m.id === materialId ? { ...m, ...patch } : m));
  store.set({ materials: { ...s.materials, [personId]: { ...prev, items } } });
}

export function removeMaterial(personId, materialId) {
  const s = store.get();
  const prev = s.materials[personId];
  if (!prev) return;
  store.set({ materials: { ...s.materials, [personId]: { ...prev, items: prev.items.filter((m) => m.id !== materialId) } } });
}

export function invalidateMaterials(personId) {
  const s = store.get();
  if (!s.materials[personId]) return;
  const next = { ...s.materials };
  delete next[personId];
  store.set({ materials: next });
}

function dedupe(items) {
  const seen = new Set();
  return items.filter((m) => (seen.has(m.id) ? false : (seen.add(m.id), true)));
}

export function addPending(slip) {
  store.set({ pending: [...store.get().pending, slip] });
}
export function removePending(id) {
  store.set({ pending: store.get().pending.filter((p) => p.id !== id) });
}

export function isEmptyAccount() {
  const s = store.get();
  return s.coreIds.length === 0 && s.casualIds.length === 0;
}

export function activePerson() {
  const s = store.get();
  return s.activePersonId ? s.people[s.activePersonId] || null : null;
}
