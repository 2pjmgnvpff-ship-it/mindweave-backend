// icons.js — constant SVG markup shipped with the app (never user data).
// Rendered only through dom.svg(), the single audited SVG markup sink.

export const ARROW_SVGS = {
  gold: `<svg viewBox="0 0 36 22" fill="none" xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="goldArrowGrad" x1="0" y1="0" x2="36" y2="22" gradientUnits="userSpaceOnUse"><stop offset="0%" stop-color="#FCE49C"/><stop offset="48%" stop-color="#D99B38"/><stop offset="100%" stop-color="#9E6816"/></linearGradient></defs><line x1="4" y1="11" x2="27" y2="11" stroke="url(#goldArrowGrad)" stroke-width="2.1" stroke-linecap="round"/><path d="M4 11L1.5 6.8L6.8 9.2M4 11L1.5 15.2L6.8 12.8M8.2 11L5.8 7.2M8.2 11L5.8 14.8" stroke="url(#goldArrowGrad)" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/><path d="M33.5 11C30.6 7.4 26.2 6.2 24.8 8.7C24.2 9.8 25.1 11 25.1 11C25.1 11 24.2 12.2 24.8 13.3C26.2 15.8 30.6 14.6 33.5 11Z" fill="url(#goldArrowGrad)"/></svg>`,
  silver: `<svg viewBox="0 0 36 22" fill="none" xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="silverArrowGrad" x1="0" y1="0" x2="36" y2="22" gradientUnits="userSpaceOnUse"><stop offset="0%" stop-color="#EEF5FC"/><stop offset="50%" stop-color="#8FA6BD"/><stop offset="100%" stop-color="#51687E"/></linearGradient></defs><line x1="4" y1="11" x2="27" y2="11" stroke="url(#silverArrowGrad)" stroke-width="2.1" stroke-linecap="round"/><path d="M4.5 11L1.8 7L7.5 11L1.8 15L4.5 11ZM8.5 11L6.2 7.5M8.5 11L6.2 14.5" stroke="url(#silverArrowGrad)" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/><polygon points="34,11 24.5,6.3 26.8,11 24.5,15.7" fill="url(#silverArrowGrad)"/></svg>`,
  crystal: `<svg viewBox="0 0 36 22" fill="none" xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="crystalPrismTop" x1="18" y1="3" x2="35" y2="12" gradientUnits="userSpaceOnUse"><stop offset="0%" stop-color="#B8FAFF"/><stop offset="52%" stop-color="#58B8FF"/><stop offset="100%" stop-color="#A67CFF"/></linearGradient><linearGradient id="crystalPrismBot" x1="18" y1="11" x2="35" y2="19" gradientUnits="userSpaceOnUse"><stop offset="0%" stop-color="#76D8FF"/><stop offset="52%" stop-color="#B589FF"/><stop offset="100%" stop-color="#FF96D5"/></linearGradient><linearGradient id="crystalShaftGrad" x1="2" y1="6" x2="26" y2="16" gradientUnits="userSpaceOnUse"><stop offset="0%" stop-color="#7AE8FF"/><stop offset="55%" stop-color="#6EA8FF"/><stop offset="100%" stop-color="#C28EFF"/></linearGradient></defs><line x1="3.8" y1="11" x2="22.5" y2="11" stroke="url(#crystalShaftGrad)" stroke-width="2.2" stroke-linecap="round"/><polygon points="2.2,11 6.2,6.2 9.4,11 6.2,15.8" fill="url(#crystalPrismTop)" opacity="0.92"/><polygon points="6.6,11 9.8,7.2 12.4,11 9.8,14.8" fill="url(#crystalPrismBot)" opacity="0.88"/><polygon points="34.8,11 25.8,4.6 21.4,11" fill="url(#crystalPrismTop)"/><polygon points="34.8,11 25.8,17.4 21.4,11" fill="url(#crystalPrismBot)"/><polygon points="34.8,11 25.8,4.6 21.4,11 25.8,17.4" stroke="#5B8CE8" stroke-width="0.75" stroke-linejoin="round"/><line x1="21.8" y1="11" x2="34.2" y2="11" stroke="#FFFFFF" stroke-width="1.05"/><circle cx="27.2" cy="8.2" r="1.05" fill="#FFFFFF"/></svg>`,
};

export const DIRECTORY_SVG = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6.5h11.5a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H4z"/><path d="M17.5 8.5H20a1 1 0 0 1 1 1v2h-3.5"/><path d="M17.5 13.5H20a1 1 0 0 1 1 1v2h-3.5"/><circle cx="9.8" cy="11.2" r="2.1"/><path d="M6.8 16.4c.6-1.5 1.7-2.2 3-2.2s2.4.7 3 2.2"/></svg>`;

export const MIC_SVG = `<svg viewBox="0 0 24 24" fill="none"><rect x="9" y="3.2" width="6" height="11.2" rx="3" stroke="currentColor" stroke-width="1.85"/><path d="M5.8 10.8a6.2 6.2 0 0 0 12.4 0" stroke="currentColor" stroke-width="1.85" stroke-linecap="round"/><line x1="12" y1="17.2" x2="12" y2="20.5" stroke="currentColor" stroke-width="1.85" stroke-linecap="round"/></svg>`;

export const SPARK_SVG = `<svg viewBox="0 0 24 24" fill="none"><path d="M12 3.2C12.7 7.6 16.4 11.3 20.8 12C16.4 12.7 12.7 16.4 12 20.8C11.3 16.4 7.6 12.7 3.2 12C7.6 11.3 11.3 7.6 12 3.2Z" stroke="currentColor" stroke-width="1.75" stroke-linejoin="round" fill="rgba(255,255,255,0.32)"/><circle cx="18.2" cy="5.8" r="1.25" fill="currentColor"/></svg>`;

export const SUNBURST_SVG = `<svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><circle cx="16" cy="16" r="3" fill="currentColor"/><line x1="16" y1="2" x2="16" y2="7"/><line x1="16" y1="25" x2="16" y2="30"/><line x1="2" y1="16" x2="7" y2="16"/><line x1="25" y1="16" x2="30" y2="16"/><line x1="6.1" y1="6.1" x2="9.6" y2="9.6"/><line x1="22.4" y1="22.4" x2="25.9" y2="25.9"/><line x1="25.9" y1="6.1" x2="22.4" y2="9.6"/><line x1="9.6" y1="22.4" x2="6.1" y2="25.9"/></svg>`;

// Brass clip rewritten with relative `v` commands (same shape as the reference).
export const BRASS_CLIP_SVG = `<svg viewBox="0 0 44 82" fill="none"><defs><linearGradient id="brassGrad" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="#F6E2A8"/><stop offset="45%" stop-color="#C99A49"/><stop offset="80%" stop-color="#8F6523"/><stop offset="100%" stop-color="#E5C478"/></linearGradient></defs><path d="M13 24 v36 C13 69.5 31 69.5 31 60 v-45 C31 7.5 17 7.5 17 15 v37 C17 56.5 26 56.5 26 52 v-26" stroke="url(#brassGrad)" stroke-width="3.2" stroke-linecap="round"/></svg>`;

export const GEAR_SVG = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5v.2a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1h-.2a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3h.1a1.7 1.7 0 0 0 1-1.5v-.2a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8v.1a1.7 1.7 0 0 0 1.5 1h.2a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>`;

export const BACK_SVG = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 5l-7 7 7 7"/></svg>`;

export const SEND_SVG = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19 v-14"/><path d="M5.5 11.5L12 5l6.5 6.5"/></svg>`;

export const PAPERS_SVG = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="5" width="12" height="15" rx="2"/><path d="M8 2.8h10a2 2 0 0 1 2 2v13"/><path d="M7.5 10h5M7.5 13.5h5"/></svg>`;

export const VOICE_ARC_SVG = `<svg class="voice-split-arc-svg" viewBox="0 0 384 838" fill="none" aria-hidden="true"><path class="voice-inner-whisper-arc" d="M 52.2 715.2 A 78 78 0 0 1 124.8 787.8"/><path class="voice-split-arc-seg seg-up" d="M 56.3 659.3 A 134 134 0 0 1 129.5 687.4"/><path class="voice-split-arc-seg seg-right" d="M 149.6 706.9 A 134 134 0 0 1 180.3 779.0"/></svg>`;

export const SEARCH_SVG = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.85" stroke-linecap="round" stroke-linejoin="round"><circle cx="10.8" cy="10.8" r="6.3"/><path d="M15.6 15.6l4.4 4.4"/></svg>`;

export const CLOSE_SVG = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.95" stroke-linecap="round"><path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/></svg>`;

export const CAMERA_SVG = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><rect x="3.5" y="5.5" width="17" height="13" rx="2.8"/><circle cx="9.2" cy="10.4" r="1.4"/><path d="M5.5 16.5l4.2-3.8 3.2 2.8 2.6-2.4 3 3.4"/></svg>`;

export const ARROW_PIN_IMG = {
  gold: './assets/cupid/arrow_pin_gold.webp',
  silver: './assets/cupid/arrow_pin_silver.webp',
  crystal: './assets/cupid/arrow_pin_crystal.webp',
};

export const CUPID_IMG = './assets/cupid/muse_cupid_a_listen.webp';
