// store.js — Single reactive app store with 6-person materials LRU, V2 scale & privacy state (<= 230L).

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
    subscribe(fn) { subs.add(fn); return () => subs.delete(fn); },
  };
}

export const EDITIONS = ['cotton', 'hobonichi', 'museglass'];
export const MAX_CACHED_PEOPLE = 6;
const materialsLruOrder = [];

function touchLru(personId) {
  const idx = materialsLruOrder.indexOf(personId);
  if (idx >= 0) materialsLruOrder.splice(idx, 1);
  materialsLruOrder.push(personId);
}

function evictMaterialsLru(materialsMap, activeId) {
  const next = { ...materialsMap };
  while (materialsLruOrder.length > MAX_CACHED_PEOPLE) {
    const candidateIdx = materialsLruOrder.findIndex((id) => id !== activeId);
    if (candidateIdx < 0) break;
    const [evicted] = materialsLruOrder.splice(candidateIdx, 1);
    delete next[evicted];
  }
  return next;
}

export const store = createStore({
  ready: false,
  people: {},
  coreIds: [],
  casualIds: [],
  view: 'journal',
  scale: 'paper',
  activePersonId: null,
  lastDemotedId: null,
  edition: 'cotton',
  preferences: null,
  inboxCount: 0,
  questionCount: 0,
  materials: {},
  inflightMaterials: {},
  bucketsByPerson: {},
  historyStack: [],
  chatDraftsByPerson: {},
  silverVeilByPerson: {},
  silverSessionNoticed: false,
  swrSnapshot: null,
  pending: [],
  draftCount: 0,
  journalFilter: 'all',
  searchOpen: false,
  searchQuery: '',
});

export function applyBootstrap(data) {
  const people = { ...(data?.people || {}) };
  const rawEdition =
    data?.preferences?.preferredEdition ||
    data?.preferences?.edition ||
    data?.preferences?.journalEdition ||
    store.get().edition;
  const edition = EDITIONS.includes(rawEdition) ? rawEdition : 'cotton';
  store.set({
    ready: true,
    people,
    coreIds: (data?.coreIds || []).filter((id) => people[id]),
    casualIds: (data?.casualIds || []).filter((id) => people[id]),
    preferences: data?.preferences || null,
    edition,
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

export function removePerson(personId) {
  if (!personId) return;
  const s = store.get();
  const people = { ...s.people };
  delete people[personId];
  const materials = { ...s.materials };
  delete materials[personId];
  const coreIds = (s.coreIds || []).filter((id) => id !== personId);
  const casualIds = (s.casualIds || []).filter((id) => id !== personId);
  const activePersonId = s.activePersonId === personId ? (coreIds[0] || casualIds[0] || null) : s.activePersonId;
  store.set({ people, materials, coreIds, casualIds, activePersonId });
}

export function applyTierChange({ personId, tier, coreIds, casualIds }) {
  const s = store.get();
  const p = s.people[personId];
  let people = s.people;
  if (p) {
    const next = { ...p, tier };
    if (p.tier !== tier) {
      delete next.sealBg;
      delete next.sealColor;
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
  touchLru(personId);
  const nextMaterials = evictMaterialsLru({
    ...s.materials,
    [personId]: {
      items,
      nextCursor: page.nextCursor || null,
      hasMore: Boolean(page.hasMore),
      loaded: true,
      loading: false,
    },
  }, s.activePersonId);
  store.set({ materials: nextMaterials });
}

export function upsertMaterial(personId, material) {
  if (!personId || !material?.id) return;
  const s = store.get();
  const prev = s.materials[personId] || { items: [], nextCursor: null, hasMore: false, loaded: true, loading: false };
  const exists = prev.items.some((m) => m.id === material.id);
  const items = exists
    ? prev.items.map((m) => (m.id === material.id ? { ...m, ...material } : m))
    : [material, ...prev.items];
  touchLru(personId);
  store.set({ materials: { ...s.materials, [personId]: { ...prev, items } } });
}

export function patchMaterial(personId, materialId, patch) {
  const s = store.get();
  const prev = s.materials[personId];
  if (!prev) return;
  store.set({ materials: { ...s.materials, [personId]: { ...prev, items: prev.items.map((m) => (m.id === materialId ? { ...m, ...patch } : m)) } } });
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
  const idx = materialsLruOrder.indexOf(personId);
  if (idx >= 0) materialsLruOrder.splice(idx, 1);
  store.set({ materials: next });
}

export function setBuckets(personId, bucketPayload) {
  if (!personId) return;
  store.set({ bucketsByPerson: { ...store.get().bucketsByPerson, [personId]: bucketPayload } });
}

export function saveChatDraft(personId, text) {
  if (!personId) return;
  store.set({ chatDraftsByPerson: { ...store.get().chatDraftsByPerson, [personId]: String(text || '') } });
}

export function toggleSilverVeil(personId, forceValue) {
  if (!personId) return false;
  const s = store.get();
  const next = typeof forceValue === 'boolean' ? forceValue : !Boolean(s.silverVeilByPerson[personId]);
  store.set({ silverVeilByPerson: { ...s.silverVeilByPerson, [personId]: next } });
  return next;
}

export function pushHistory(entry) {
  store.set({ historyStack: [...store.get().historyStack.slice(-15), entry] });
}

export function popHistory() {
  const s = store.get();
  if (s.historyStack.length === 0) return null;
  const top = s.historyStack[s.historyStack.length - 1];
  store.set({ historyStack: s.historyStack.slice(0, -1) });
  return top;
}

export function resetRuntimeCaches() {
  materialsLruOrder.length = 0;
  store.set({
    materials: {}, inflightMaterials: {}, bucketsByPerson: {}, historyStack: [],
    chatDraftsByPerson: {}, silverVeilByPerson: {}, silverSessionNoticed: false,
    swrSnapshot: null, pending: [], searchOpen: false, searchQuery: '', scale: 'paper',
  });
}

function dedupe(items) {
  const seen = new Set();
  return items.filter((m) => (seen.has(m.id) ? false : (seen.add(m.id), true)));
}

export function addPending(slip) { store.set({ pending: [...store.get().pending, slip] }); }
export function removePending(id) { store.set({ pending: store.get().pending.filter((p) => p.id !== id) }); }
export function isEmptyAccount() { const s = store.get(); return s.coreIds.length === 0 && s.casualIds.length === 0; }
export function activePerson() { const s = store.get(); return s.activePersonId ? s.people[s.activePersonId] || null : null; }
