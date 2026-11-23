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
  transcribeVoice: ({ blob, filename, personId }) => {
    const form = new FormData();
    form.append('audio', blob, filename || 'voice.webm');
    if (personId) form.append('active_person_id', personId);
    return client.request('POST', '/voice/transcribe', { form, idem: true });
  },
  captureImage: ({ id, file, text, routeMode, activePersonId }, idem) => {
    const form = new FormData();
    form.append('image', file, file?.name || 'image.jpg');
    form.append('source', 'image');
    form.append('sync', 'true');
    form.append('routeMode', routeMode || 'global');
    if (activePersonId) form.append('activePersonId', activePersonId);
    if (text) form.append('text', text);
    if (id) form.append('id', id);
    return client.request('POST', '/captures', { query: { sync: 'true' }, form, idem: idem || true });
  },
  undoCapture: (captureId) => client.post(`/captures/${encodeURIComponent(captureId)}:undo`, {}),

  // §7 materials, facts, reminders, brief, inbox
  brief: (personId) => client.get(`/people/${encodeURIComponent(personId)}/brief`),
  materials: (personId, { cursor, limit = 30, includeHeld } = {}) =>
    client.get(`/persons/${encodeURIComponent(personId)}/materials`, { query: { cursor, limit, includeHeld } }),
  createMaterial: (personId, { kind = 'note', title, body, pinned = false } = {}) => {
    const text = String(body || '').trim();
    return client.post(`/persons/${encodeURIComponent(personId)}/materials`, {
      kind,
      title: String(title || text).slice(0, 80),
      body: text,
      pinned: Boolean(pinned),
    });
  },
  uploadPhoto: (personId, { file, caption, pinned = false }) => {
    const form = new FormData();
    const note = String(caption || '').trim() || '拍下的一张照片';
    form.append('photo', file, file?.name || 'photo.jpg');
    form.append('kind', 'photo');
    form.append('title', note.slice(0, 80));
    form.append('body', note);
    form.append('pinned', String(Boolean(pinned)));
    return client.request('POST', `/persons/${encodeURIComponent(personId)}/materials`, { form, idem: true });
  },
  patchMaterial: (materialId, body, version) =>
    client.patch(
      `/materials/${encodeURIComponent(materialId)}`,
      body,
      version != null ? { headers: { 'If-Match': `"${version}"` } } : {},
    ),
  deleteMaterial: (materialId) => client.request('DELETE', `/materials/${encodeURIComponent(materialId)}`),
  facts: (personId, query = {}) =>
    client.get(`/persons/${encodeURIComponent(personId)}/facts`, { query }),
  reminders: (personId, query = {}) =>
    client.get('/reminders', { query: { personId, ...query } }),
  createReminder: (personId, { title, triggerDate, rule, factId, leadDays } = {}) =>
    client.post(`/people/${encodeURIComponent(personId)}/reminders`, {
      title,
      triggerDate,
      ...(rule !== undefined ? { rule } : {}),
      ...(factId !== undefined ? { factId } : {}),
      ...(leadDays !== undefined ? { leadDays } : {}),
    }),
  patchReminder: (reminderId, body, version) =>
    client.patch(
      `/reminders/${encodeURIComponent(reminderId)}`,
      body,
      version != null ? { headers: { 'If-Match': `"${version}"` } } : {},
    ),
  /** PATCH a fact; the backend requires If-Match with the fact version (contract §7). */
  patchFact: (factId, body, version) =>
    client.patch(`/facts/${encodeURIComponent(factId)}`, body, version != null ? { headers: { 'If-Match': `"${version}"` } } : {}),
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

  // §10 meta
  aiStatus: () => client.get('/meta/ai-status'),
};

export { uuid };
