// endpoints.js — thin, typed-ish wrappers around the P0 contract (docs/P0_FRONTEND_CONTRACT.md).
import { createClient, uuid } from './client.js';
import { readSseResponse } from './sse.js';

export const client = createClient();

export const api = {
  // §3 auth
  requestOtp: (email) => client.post('/auth/email/otp:request', { email }, { auth: false, idem: false }),
  verifyOtp: (challengeId, email, code) =>
    client.post('/auth/email/otp:verify', { challengeId, email, code }, { auth: false, idem: false }),
  async logout() {
    const t = client.getTokens();
    try {
      await client.post('/auth/logout', { refreshToken: t?.refreshToken }, { idem: false });
    } catch {
      /* best effort */
    }
    client.setTokens(null);
  },
  grantAiConsent: () =>
    client.post('/me/consents', { consentKind: 'ai_processing', policyVersion: '2026-11', granted: true }),

  // §4 bootstrap / §9 preferences
  bootstrap: () => client.get('/bootstrap'),
  patchPreferences: (patch) => client.patch('/me/preferences', patch),
  requestExport: () => client.post('/me/exports', {}),

  // §5 persons
  createPerson: (body) => client.post('/persons', body),
  setTier: (personId, tier) => client.patch(`/persons/${encodeURIComponent(personId)}/tier`, { tier }),

  // §6 capture
  captureText: ({ id, text, routeMode, activePersonId }, idem) =>
    client.request('POST', '/captures', {
      query: { sync: 'true' },
      body: { id, source: 'text', text, routeMode, activePersonId: activePersonId || undefined },
      idem: idem || true,
    }),
  ingestVoice: ({ id, blob, filename, routeMode, activePersonId }, idem) => {
    const form = new FormData();
    form.append('audio', blob, filename || 'voice.webm');
    form.append('routeMode', routeMode);
    if (activePersonId) form.append('activePersonId', activePersonId);
    if (id) form.append('id', id);
    return client.request('POST', '/voice/ingest', { form, idem: idem || true });
  },
  undoCapture: (captureId) => client.post(`/captures/${encodeURIComponent(captureId)}:undo`, {}),

  // §7 materials, facts, inbox
  materials: (personId, { cursor, limit = 30, includeHeld } = {}) =>
    client.get(`/persons/${encodeURIComponent(personId)}/materials`, { query: { cursor, limit, includeHeld } }),
  patchFact: (factId, body) => client.patch(`/facts/${encodeURIComponent(factId)}`, body),
  inbox: (query = {}) => client.get('/inbox', { query: { status: 'open', ...query } }),
  resolveInbox: (itemId, body) => client.post(`/inbox/${encodeURIComponent(itemId)}:resolve`, body),

  // §8 chat
  chatHistory: (personId, { cursor, limit = 40 } = {}) =>
    client.get(`/persons/${encodeURIComponent(personId)}/chat/messages`, { query: { order: 'asc', limit, cursor } }),
  /**
   * Stream a Cupid reply. Returns a promise resolving when the stream ends.
   * `idem` lets a soft retry reuse the same Idempotency-Key.
   */
  async streamChat(personId, { message, referencedMaterialIds = [], clientMessageId, idem, signal }, onEvent) {
    const res = await client.request('POST', `/persons/${encodeURIComponent(personId)}/chat/stream`, {
      body: { message, referencedMaterialIds, clientMessageId: clientMessageId || uuid() },
      idem: idem || true,
      raw: true,
      signal,
    });
    await readSseResponse(res, onEvent);
  },
};

export { uuid };
