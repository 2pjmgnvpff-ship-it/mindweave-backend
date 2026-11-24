// actions.js — Acyclic cross-view action registry (<= 45L).
// main.js fills these in at boot via lazy dynamic imports so Lane C-2 and Lane C-3 never circularly import.

const noop = () => {};

export const actions = {
  openPerson: noop,          // (personId, { materialId?, fromDirectory?, monthKey? })
  openDirectory: noop,       // legacy alias for openPeopleSheet
  openPeopleSheet: noop,     // (opts?)
  openChat: noop,            // (personId, opts?)
  closeChat: noop,           // ()
  openPeek: noop,            // (materialId, person, opts?)
  openTextCapture: noop,     // ({ personId?, prompt?, directory? })
  openArchiveHistory: noop,  // ({ filter? })
  openCaptureReceipt: noop,  // (res, meta?)
  openSettings: noop,        // ()
  openDataRights: noop,      // ('export' | 'delete')
  openCrisisCard: noop,      // (payload)
  showCrisis: noop,          // legacy alias for openCrisisCard
  reloadMaterials: noop,     // (personId)
  refreshBootstrap: noop,    // () => Promise
  flyAvatars: noop,          // (splits)
  stopActiveAudio: noop,     // ()
};
