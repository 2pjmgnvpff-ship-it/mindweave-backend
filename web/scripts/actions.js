// actions.js — tiny cross-view action registry (avoids circular imports between views).
// main.js fills these in at boot; views call them, e.g. actions.openPerson(id).
const noop = () => {};

export const actions = {
  openPerson: noop, // (personId, { materialId?, fromDirectory? })
  openDirectory: noop,
  openChat: noop, // (personId)
  closeChat: noop,
  openTextCapture: noop, // ({ personId?, prompt?, directory? })
  reloadMaterials: noop, // (personId)
  refreshBootstrap: noop, // () => Promise
  showCrisis: noop, // (crisisCard)
  flyAvatars: noop, // (splits)
};
