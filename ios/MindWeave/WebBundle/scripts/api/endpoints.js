// endpoints.js — Canonical V2 MindWeave API endpoints aligned 1:1 with live FastAPI OpenAPI (BC6, <= 220L).

import { createClient, uuid } from './client.js';
import { readSseResponse } from './sse.js';

export const client = createClient();

function normalizeSearchHit(hit) {
  if (!hit || typeof hit !== 'object') return hit;
  if (hit.material && typeof hit.material === 'object') {
    return {
      ...hit.material, material: hit.material, score: hit.score, matchKind: hit.matchKind,
      matchedTerms: Array.isArray(hit.matchedTerms) ? hit.matchedTerms : [],
      highlightSnippet: hit.highlightSnippet || '',
      matchedFactSnippets: Array.isArray(hit.matchedFactSnippets) ? hit.matchedFactSnippets : [],
    };
  }
  return hit;
}

export const api = {
  // Auth & Step-Up Reauth
  requestOtp: (email, purpose) =>
    client.post('/auth/email/otp:request', purpose ? { email, purpose } : { email }, { auth: false, idem: false }),
  requestEmailOtp: (email, purpose) => api.requestOtp(email, purpose),
  verifyOtp: (challengeId, email, code, purpose) =>
    client.post('/auth/email/otp:verify', purpose ? { challengeId, email, code, purpose } : { challengeId, email, code }, { auth: false, idem: false }),
  requestReauthOtp: (email) => client.post('/auth/email/otp:request', { email, purpose: 'reauth' }, { idem: false }),
  verifyReauthOtp: (challengeId, email, code) => client.post('/auth/email/otp:verify', { challengeId, email, code, purpose: 'reauth' }, { idem: false }),
  reauthApple: (identityToken) => client.post('/auth/reauth/apple', { identityToken }, { idem: false }),
  appleLogin: (body) => client.post('/auth/apple', body, { auth: false, idem: false }),
  async logout() {
    const t = client.getTokens();
    try { await client.post('/auth/logout', { refreshToken: t?.refreshToken }, { idem: false }); } catch { /* best effort */ }
    client.setTokens(null);
  },
  grantAiConsent: () => client.post('/me/consents', { consentKind: 'ai_processing', policyVersion: '2026-11', granted: true }),

  // Bootstrap, Preferences & Data Rights
  bootstrap: () => client.get('/bootstrap'),
  patchPreferences: (patch) => client.patch('/me/preferences', patch),
  createExport: (reauthToken, idem) =>
    client.post('/me/exports', {}, { headers: reauthToken ? { 'X-Reauth-Token': reauthToken } : {}, idem: idem || true }),
  requestExport: (reauthToken, idem) => api.createExport(reauthToken, idem),
  async downloadExportBlob(downloadPath, { signal } = {}) {
    const res = await client.request('GET', downloadPath, { raw: true, accept: 'application/zip, application/octet-stream, */*', signal });
    return res.blob();
  },
  deleteAccount: (reauthToken, body = {}) =>
    client.request('DELETE', '/me', { body, headers: reauthToken ? { 'X-Reauth-Token': reauthToken } : {}, idem: true }),

  // Persons
  createPerson: (body) => client.post('/persons', body),
  patchPerson: (personId, body) => client.patch(`/persons/${encodeURIComponent(personId)}`, body),
  setTier: (personId, tier) => client.patch(`/persons/${encodeURIComponent(personId)}/tier`, { tier }),

  // Captures & C1 Archive History
  captureText: ({ id, text, routeMode, activePersonId }, idem) =>
    client.request('POST', '/captures', {
      query: { sync: 'true' }, body: { id, source: 'text', text, routeMode, activePersonId: activePersonId || undefined }, idem: idem || true,
    }),
  ingestVoice: ({ id, blob, filename, routeMode, activePersonId, clientTranscript, speechEngine, audioDurationMs }, idem) => {
    const form = new FormData();
    if (blob) form.append('audio', blob, filename || 'voice.m4a');
    form.append('routeMode', routeMode || 'global');
    if (activePersonId) form.append('activePersonId', activePersonId);
    if (id) form.append('id', id);
    if (clientTranscript) form.append('clientTranscript', String(clientTranscript));
    if (speechEngine) form.append('speechEngine', String(speechEngine));
    if (audioDurationMs != null && Number.isFinite(Number(audioDurationMs))) form.append('audioDurationMs', String(Math.round(Number(audioDurationMs))));
    return client.request('POST', '/voice/ingest', { form, idem: idem || true });
  },
  transcribeVoice: ({ blob, filename, personId }) => {
    const form = new FormData();
    form.append('audio', blob, filename || 'voice.m4a');
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
  async listCaptures({ limit = 20, cursor, filter, source, includeReverted = false, signal } = {}) {
    const resolvedSource = source || (filter === 'voice' ? 'voice' : undefined);
    const res = await client.get('/captures', { query: { limit, cursor, source: resolvedSource, includeReverted: includeReverted ? 'true' : undefined }, signal });
    if (filter === 'pending' && Array.isArray(res?.items)) return { ...res, items: res.items.filter((it) => Number(it?.pendingCount || 0) > 0) };
    return res;
  },
  deleteCapture: (captureId) => client.request('DELETE', `/captures/${encodeURIComponent(captureId)}`, { idem: true }),
  undoCapture: (captureId) => client.request('DELETE', `/captures/${encodeURIComponent(captureId)}`, { idem: true }),
  addCaptureSlip: (captureId, body) => client.post(`/captures/${encodeURIComponent(captureId)}/slips`, body),
  createCaptureSlip: (captureId, body) => client.post(`/captures/${encodeURIComponent(captureId)}/slips`, body),

  // Materials, 3-Scale Timeline Buckets & B1 Search
  brief: (personId) => client.get(`/people/${encodeURIComponent(personId)}/brief`),
  timelineBuckets: (personId, query = {}) => client.get(`/persons/${encodeURIComponent(personId)}/timeline-buckets`, { query }),
  materials: (personId, { cursor, limit = 30, includeHeld, year, month, from, to, kind, pinned, signal } = {}) =>
    client.get(`/persons/${encodeURIComponent(personId)}/materials`, { query: { cursor, limit, includeHeld, year, month, from, to, kind, pinned }, signal }),
  materialsByMonth: (personId, year, month, opts = {}) => api.materials(personId, { ...opts, year, month }),
  async personSearch(personId, { q, facet, kind, pinned, year, month, from, to, includeHeld, limit = 40, signal } = {}) {
    const query = { q, year, month, from, to, includeHeld, limit };
    if (pinned !== undefined) query.pinned = pinned;
    else if (facet === 'pinned') query.pinned = 'true';
    if (kind) query.kind = kind;
    else if (facet === 'photo' || facet === 'voice' || facet === 'note') query.kind = facet;
    const res = await client.get(`/persons/${encodeURIComponent(personId)}/search`, { query, signal });
    return res && Array.isArray(res.items) ? { ...res, items: res.items.map(normalizeSearchHit) } : res;
  },
  createMaterial: (personId, { kind = 'note', title, body, pinned = false, happenedOn, captureId, photoBase64 } = {}) => {
    const text = String(body || '').trim();
    const payload = { kind, title: String(title || text).slice(0, 80), body: text, pinned: Boolean(pinned) };
    if (happenedOn) payload.happenedOn = happenedOn;
    if (captureId) payload.captureId = captureId;
    if (photoBase64) payload.photoBase64 = photoBase64;
    return client.post(`/persons/${encodeURIComponent(personId)}/materials`, payload);
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
    client.patch(`/materials/${encodeURIComponent(materialId)}`, body, version != null ? { headers: { 'If-Match': `"${version}"` } } : {}),
  deleteMaterial: (materialId) => client.request('DELETE', `/materials/${encodeURIComponent(materialId)}`),
  async fetchMediaBlob(mediaUrl, { signal } = {}) {
    const res = await client.request('GET', mediaUrl, { raw: true, accept: '*/*', signal });
    return res.blob();
  },

  // Facts, Reminders & Inbox
  facts: (personId, query = {}) => client.get(`/persons/${encodeURIComponent(personId)}/facts`, { query }),
  patchFact: (factId, body, version) =>
    client.patch(`/facts/${encodeURIComponent(factId)}`, body, version != null ? { headers: { 'If-Match': `"${version}"` } } : {}),
  reminders: (personId, query = {}) => client.get('/reminders', { query: { personId, ...query } }),
  createReminder: (personId, {
    title, onceDate, triggerDate, rule, isLunar, targetMonth, lunarMonth, targetDay, lunarDay,
    isLeapMonth, factId, leadDays, fireHour, fireMinute, timezone,
  } = {}) => {
    const payload = { title };
    const resolvedOnce = onceDate ?? triggerDate;
    const resolvedMonth = targetMonth ?? lunarMonth;
    const resolvedDay = targetDay ?? lunarDay;
    if (resolvedOnce !== undefined) payload.onceDate = resolvedOnce;
    if (rule !== undefined) payload.rule = rule;
    if (isLunar !== undefined) payload.isLunar = Boolean(isLunar);
    if (resolvedMonth !== undefined) payload.targetMonth = Number(resolvedMonth);
    if (resolvedDay !== undefined) payload.targetDay = Number(resolvedDay);
    if (isLeapMonth !== undefined) payload.isLeapMonth = Boolean(isLeapMonth);
    if (factId !== undefined) payload.factId = factId;
    if (leadDays !== undefined) payload.leadDays = leadDays;
    if (fireHour !== undefined) payload.fireHour = fireHour;
    if (fireMinute !== undefined) payload.fireMinute = fireMinute;
    if (timezone !== undefined) payload.timezone = timezone;
    return client.post(`/people/${encodeURIComponent(personId)}/reminders`, payload);
  },
  patchReminder: (reminderId, body, version) =>
    client.patch(`/reminders/${encodeURIComponent(reminderId)}`, body, version != null ? { headers: { 'If-Match': `"${version}"` } } : {}),
  updateReminder: (reminderId, body, version) => api.patchReminder(reminderId, body, version),
  markReminderDone: (reminderId) => client.post(`/reminders/${encodeURIComponent(reminderId)}:done`, {}),
  inbox: (query = {}) => client.get('/inbox', { query: { status: 'open', ...query } }),
  resolveInbox: (itemId, body) => client.post(`/inbox/${encodeURIComponent(itemId)}:resolve`, body),

  // Chat (BC3 BUG-CC-5 default order: 'desc', limit: 40; forwards AbortSignal into readSseResponse)
  chatHistory: (personId, { cursor, limit = 40, order = 'desc' } = {}) =>
    client.get(`/persons/${encodeURIComponent(personId)}/chat/messages`, { query: { order, limit, cursor } }),
  async streamChat(personId, { message, referencedMaterialIds = [], clientMessageId, idem, signal }, onEvent) {
    const res = await client.request('POST', `/persons/${encodeURIComponent(personId)}/chat/stream`, {
      body: { message, referencedMaterialIds, clientMessageId: clientMessageId || uuid() },
      idem: idem || true, raw: true, accept: 'text/event-stream, application/json', signal,
    });
    await readSseResponse(res, onEvent, { signal });
  },
  aiStatus: () => client.get('/meta/ai-status'),
};

export { uuid };
