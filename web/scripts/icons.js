// icons.js — constant 1.55px hairline SVG markup shipped with the app (explicit 18x18 default sizing, <= 100L).
// Rendered only through dom.svg(), the single audited SVG markup sink.

export const ARROW_SVGS = {
  gold: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round"><path d="M5 19L19 5"/><path d="M11 5h8v8"/><path d="M4.5 14.5l4.8 4.8"/></svg>`,
  silver: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3.5l7 3v5.4c0 4.3-3 7.5-7 8.6-4-1.1-7-4.3-7-8.6V6.5l7-3z"/></svg>`,
  crystal: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3.5l7.5 8.5L12 20.5 4.5 12 12 3.5z"/><path d="M4.5 12h15"/></svg>`,
};

export const ARROW_DIAG_SVG = ARROW_SVGS.gold;
export const SHIELD_SVG = ARROW_SVGS.silver;
export const DIAMOND_SVG = ARROW_SVGS.crystal;

export const DIRECTORY_SVG = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4.5 6.5h11a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2h-11z"/><path d="M17.5 9H20v3h-2.5M17.5 14H20v3h-2.5"/><circle cx="10" cy="11.2" r="2"/><path d="M7.2 16.2c.6-1.4 1.6-2.1 2.8-2.1s2.2.7 2.8 2.1"/></svg>`;
export const MIC_HAIRLINE_SVG = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.55" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="3.5" width="6" height="11" rx="3"/><path d="M6 11a6 6 0 0 0 12 0"/><line x1="12" y1="17" x2="12" y2="20.5"/></svg>`;
export const MIC_SVG = MIC_HAIRLINE_SVG;
export const GEAR_SVG = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.55" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5v.2a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1h-.2a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3h.1a1.7 1.7 0 0 0 1-1.5v-.2a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8v.1a1.7 1.7 0 0 0 1.5 1h.2a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>`;
export const BACK_SVG = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M15 5.5L8.5 12l6.5 6.5"/></svg>`;
export const SEND_SVG = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 18.5v-13"/><path d="M6 11.5L12 5.5l6 6"/></svg>`;
export const STOP_SVG = `<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><rect x="7" y="7" width="10" height="10" rx="2.2"/></svg>`;
export const PLUS_SVG = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M12 5.5v13M5.5 12h13"/></svg>`;
export const PAPERS_SVG = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="4.5" y="5.5" width="11.5" height="14.5" rx="2"/><path d="M8.5 3.5h9a2 2 0 0 1 2 2v12.5"/><path d="M8 10.5h4.5M8 14h4.5"/></svg>`;
export const DRAWER_SVG = PAPERS_SVG;
export const SEARCH_SVG = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round"><circle cx="10.8" cy="10.8" r="6.2"/><path d="M15.6 15.6l4.2 4.2"/></svg>`;
export const CLOSE_SVG = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round"><path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/></svg>`;
export const CAMERA_SVG = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3.5" y="5.5" width="17" height="13" rx="2.6"/><circle cx="9.2" cy="10.4" r="1.4"/><path d="M5.5 16.5l4.2-3.8 3.2 2.8 2.6-2.4 3 3.4"/></svg>`;
export const BELL_SVG = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M6.2 15.5c1.1-1.1 1.6-2.4 1.6-4.6a4.2 4.2 0 1 1 8.4 0c0 2.2.5 3.5 1.6 4.6a.9.9 0 0 1-.65 1.5H6.85a.9.9 0 0 1-.65-1.5Z"/><path d="M9.8 19.2a2.4 2.4 0 0 0 4.4 0"/></svg>`;
export const PEN_SVG = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M15.8 4.6a2.1 2.1 0 0 1 3 3L8.2 18.2l-4.2 1.2 1.2-4.2L15.8 4.6Z"/><path d="M13.6 6.8l3.6 3.6"/></svg>`;
export const AUDIO_NOTE_SVG = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M9.5 17.5V6.6l9-2.1v10.8"/><circle cx="7" cy="17.5" r="2.4"/><circle cx="16" cy="15.3" r="2.4"/></svg>`;
export const BOOKMARK_HAIRLINE_SVG = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round"><path d="M6.5 4.5h11a1 1 0 0 1 1 1v14.2l-6.5-3.9-6.5 3.9V5.5a1 1 0 0 1 1-1z"/></svg>`;
export const CHEVRON_DOWN_SVG = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M6.5 9.5l5.5 5.5 5.5-5.5"/></svg>`;
export const CHEVRON_RIGHT_SVG = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M9.5 6.5l5.5 5.5-5.5 5.5"/></svg>`;
export const PLAY_SVG = `<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M8.5 6.4l9.5 5.6-9.5 5.6V6.4z"/></svg>`;
export const PAUSE_SVG = `<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><rect x="7" y="6" width="3.5" height="12" rx="1"/><rect x="13.5" y="6" width="3.5" height="12" rx="1"/></svg>`;
export const CHECK_SVG = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M5.5 12.5l4.2 4.2 8.8-9.2"/></svg>`;
export const EYE_OFF_SVG = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4l16 16"/><path d="M10.6 6.2A8.5 8.5 0 0 1 20.5 12a9.8 9.8 0 0 1-2.6 3.3"/><path d="M6.6 7.5A9.7 9.7 0 0 0 3.5 12c1.6 3.2 4.8 5.5 8.5 5.5 1.3 0 2.5-.3 3.6-.8"/></svg>`;
export const FACEID_SVG = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.55" stroke-linecap="round" stroke-linejoin="round"><path d="M7.5 4H5.8A1.8 1.8 0 0 0 4 5.8v1.7M16.5 4h1.7A1.8 1.8 0 0 1 20 5.8v1.7M4 16.5v1.7A1.8 1.8 0 0 0 5.8 20h1.7M20 16.5v1.7a1.8 1.8 0 0 1-1.8 1.8h-1.7"/><path d="M9 9.5v1.5M15 9.5v1.5M12 9.5v3.2h-.8M9.3 15.2a3.8 3.8 0 0 0 5.4 0"/></svg>`;
export const HISTORY_SVG = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4.5 12a7.5 7.5 0 1 0 2.2-5.3L4.5 9"/><path d="M4.5 5.5V9H8"/><path d="M12 8.5v4l2.8 1.6"/></svg>`;
export const TRASH_SVG = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M5 7h14M9.5 7V5a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v2M7.5 7l.8 12a1.5 1.5 0 0 0 1.5 1.4h4.4a1.5 1.5 0 0 0 1.5-1.4L16.5 7"/></svg>`;
export const COPY_SVG = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="8.5" y="8.5" width="11" height="11" rx="2"/><path d="M5.5 15.5a1.5 1.5 0 0 1-1.5-1.5V6a2 2 0 0 1 2-2h8a1.5 1.5 0 0 1 1.5 1.5"/></svg>`;
export const SWAP_SVG = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M7 7h11l-3-3M17 17H6l3 3"/></svg>`;
export const SHARE_SVG = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 15V4.5M8 8.5l4-4 4 4"/><path d="M5.5 12.5v6a1.5 1.5 0 0 0 1.5 1.5h10a1.5 1.5 0 0 0 1.5-1.5v-6"/></svg>`;

export const ARROW_PIN_IMG = {
  gold: './assets/cupid/arrow_pin_gold.webp',
  silver: './assets/cupid/arrow_pin_silver.webp',
  crystal: './assets/cupid/arrow_pin_crystal.webp',
};
export const CUPID_IMG = './assets/cupid/muse_cupid_a_listen.webp';
