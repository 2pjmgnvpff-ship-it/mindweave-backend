# MindWeave P0 Frontend ⇄ Backend Contract

> Status: **v0 (draft, landing incrementally)**. Sections marked 🆕 are being added in the
> P0 backend sprint (pencil drafts / D1). Everything else already exists on `main` and is
> covered by `openapi/openapi.json`. When this file changes, the frontend agent is notified.

## 0. Conventions

| Topic | Rule |
|---|---|
| Base URL | Same origin as the PWA. Canonical prefix **`/v1`** (alias `/api/v1` also works). |
| JSON casing | **camelCase** on the wire, both directions. |
| Auth | `Authorization: Bearer <accessToken>` on every `/v1/*` call except `/auth/*` and signed media URLs. |
| Idempotency | Every mutating call (`POST`/`PATCH`/`DELETE`) **must** send `Idempotency-Key: <uuid v4>` (1..128 chars). Retries with the same key + same body replay the original response. `/voice/ingest` accepts it optionally (send it anyway – offline retry). |
| Optimistic concurrency | `PATCH` on persons/materials/facts/preferences/reminders accepts optional `If-Match: "<version>"`; responses carry `ETag: "<version>"`. |
| Errors | RFC 7807 `application/problem+json`: `{"type","title","status","code","detail","instance","requestId"}`. Branch on `code` (e.g. `validation_failed`, `unauthorized`, `not_found`, `conflict`, `rate_limited`, `quota_exceeded`, `payload_too_large`). Never show `detail` verbatim to end users. |
| IDs | UUID strings. The client may pre-generate `id` for creates (optimistic rendering). **Never render a UUID in the UI.** |
| Dates | `YYYY-MM-DD` for calendar dates, ISO-8601 with offset for timestamps. |
| Request id | Every response has `X-Request-Id`. |

## 1. Running the backend locally (dev)

```bash
# 1) PostgreSQL 15 on 127.0.0.1:55432 (same cluster the test-suite uses)
/usr/lib/postgresql/15/bin/pg_ctl -D /home/antigravity/.pgdata -l /home/antigravity/.pgdata/pg.log \
  -o "-p 55432 -h 127.0.0.1 -k /tmp" start          # no-op if already running
# 2) schema
cd mindweave-backend && make migrate                   # alembic upgrade head on DB "mindweave"
# 3) API + embedded worker, dev OTP echo enabled, serves ../web at /
make dev                                               # http://127.0.0.1:8000/
```

`make dev` sets (override via env as needed):

| Env var | Dev value | Meaning |
|---|---|---|
| `MINDWEAVE_ENVIRONMENT` | `dev` | enables dev OTP echo + `/dev/workbench` + `/_v10_reference/` |
| `MINDWEAVE_EXPOSE_DEV_OTP_IN_RESPONSE` | `true` | OTP is echoed as `devOtpCode` (forbidden in staging/prod) |
| `MINDWEAVE_EMBED_WORKER_IN_API` | `true` | reminders / pencil auto-ink / capture jobs run in-process |
| `MINDWEAVE_AI_PRIMARY_PROVIDER` | `fake` | deterministic offline AI. `gemini` / `deepseek` / `dashscope` for real models |
| `MINDWEAVE_WEB_DIST_DIR` | `<repo>/web` | 🆕 directory served at `/` |

Port **8000**. The PWA is served from the same origin, so no CORS setup is needed.

## 2. Static app serving 🆕

| Path | Served |
|---|---|
| `/` | `web/index.html` (all environments) |
| `/<file>` | files under `web/` (e.g. `/app.js`, `/styles/x.css`, `/sw.js`, `/manifest.webmanifest`) |
| `/_v10_reference/**` | **dev/test only** (404 in staging/prod) – design reference, never link to it from product code |
| `/dev/workbench` | old backend workbench, dev/test only |
| `/healthz`, `/readyz`, `/docs`, `/v1/*`, `/api/v1/*` | API (unchanged) |

Security headers on static responses:
`Content-Security-Policy: default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob: data:; connect-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self' data:; worker-src 'self'; manifest-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'`,
`Permissions-Policy: microphone=(self), camera=(), geolocation=()`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`.
⇒ **No inline `<script>`**, no external CDNs, no `eval`. Inline `style=""` attributes are allowed.

## 3. Auth – email OTP (dev)

```http
POST /v1/auth/email/otp:request
{"email": "me@example.com"}                       // purpose defaults to "login"
→ 200 {"challengeId": "3f0c…", "expiresIn": 600, "devOtpCode": "482913"}   // devOtpCode only in dev

POST /v1/auth/email/otp:verify
{"challengeId": "3f0c…", "email": "me@example.com", "code": "482913"}
→ 200 {"userId": "…", "isNewUser": true, "accessToken": "eyJ…", "refreshToken": "…", "expiresIn": 900}

POST /v1/auth/refresh      {"refreshToken": "…"}  → 200 {"accessToken","refreshToken","expiresIn"}   // rotate; old refresh token is single-use
POST /v1/auth/logout       {"refreshToken": "…"}  (Bearer + Idempotency-Key not required) → 200 {"ok": true}
```
Access token TTL 15 min → refresh on `401` (`code: "unauthorized"`) once, then re-login.

## 4. Bootstrap

```http
GET /v1/bootstrap
→ 200
{
  "coreIds": ["<personId>", …],        // gold+silver, ordered
  "casualIds": ["<personId>", …],      // crystal, ordered
  "people": {
    "<personId>": {
      "id": "<personId>", "nameCn": "林晚", "monogram": "林", "tier": "gold",
      "state": "active",                 // active | provisional (AI-discovered, unconfirmed)
      "roleTag": "", "contextSub": "", "shortMemo": "", "cupidTitle": "…",
      "traits": [], "visualTokens": {}, "sealBg": "…", "sealColor": "…", "tabColor": "…", "tapeColor": "…",
      "hasPhoto": false, "avatarUrl": null, "mainPhotoUrl": null, "subPhotoUrl": null,
      "materials": [ {"id","type":"voice|note|photo","title","snippet","body","happenedOn","dur","mediaUrl","stampCode","thumb"} ],
      "version": 1
    }
  },
  "inboxCount": 0,
  "questionCount": 0,                  // 🆕 open "question" notes (who-is-this). Show the inbox entry only when > 0
  "preferences": { "preferredEdition": "cotton", "cupidLayout": "a", "wallpaperPreset": "warm_paper",
                   "hasCustomWallpaper": false, "wallpaperUrl": null, "wallpaperBlur": 12, "wallpaperVignette": 20,
                   "appLockEnabled": false, "lateNightCareEnabled": true, "timezone": "Asia/Shanghai",
                   "edition": "cotton", "version": 1 }
}
```
A brand new account gets `coreIds: [], casualIds: [], people: {}` → render the D7 cold-start ritual.

## 5. Persons

```http
GET /v1/persons                       // ?includeArchived=false&includeProvisional=true
→ 200 {"items": [PersonDTO…], "coreIds": [...], "casualIds": [...]}

POST /v1/persons          (Idempotency-Key)
{"id": "<optional client uuid>", "nameCn": "妈妈", "tier": "gold", "roleTag": "家人", "aliases": ["老妈"]}
→ 201 PersonDTO {"id","nameCn","monogram","tier","state":"active","rank","roleTag","contextSub","shortMemo",
                 "cupidTitle","traits":[],"visualTokens":{},"hasPhoto":false,"avatarUrl":null,…,"version":1}

PATCH /v1/persons/{id}    (Idempotency-Key, If-Match)   {"nameCn": "…", "state": "active", "archived": false, …}
PATCH /v1/persons/{id}/tier (Idempotency-Key)           {"tier": "silver"}
→ 200 {"personId","previousTier":"gold","tier":"silver","coreIds":[…],"casualIds":[…],"version":2}
```
Tier values: `gold` (记爱) · `silver` (记恨) · `crystal` (记繁琐).

## 6. Capture – voice & text

### 6.1 Voice (multipart) – synchronous
```http
POST /v1/voice/ingest     (Bearer, Idempotency-Key recommended)
Content-Type: multipart/form-data
  audio=<Blob webm|mp4/m4a|ogg|wav|mp3, ≤10 MB, ≤120 s>
  routeMode=global|current          // "current" = the person page that is open
  activePersonId=<uuid>             // required when routeMode=current
  id=<optional client capture uuid>
→ 200 VoiceIngestResponse
{
  "jobId": "<captureId>",
  "routeMode": "global",
  "transcriptHtml": "跟<mark>妈妈</mark>说好5月12日回家吃饭",   // only <mark> tags, already escaped
  "splits": [ CaptureSplit… ],
  "pendingCount": 1,
  "undoDeadline": "2026-11-16T19:00:05Z",      // null when nothing was written as ink
  "crisisCard": null                            // non-null → show crisis card, nothing else
}
```

### 6.2 Text
```http
POST /v1/captures?sync=true   (Idempotency-Key)
{"id": "<optional uuid>", "source": "text", "text": "她不吃香菜", "routeMode": "current", "activePersonId": "<uuid>"}
→ 201 CaptureResponse
{ "captureId", "source":"text", "routeMode", "scopedPersonId", "status":"completed|needs_review|…",
  "transcript", "transcriptHtml", "splits":[CaptureSplit…], "pendingCount":0, "inboxItemIds":[…],
  "safetyCategory":"normal", "crisisCard":null, "undoDeadline":"…", "createdAt", "completedAt" }
```
Always pass `?sync=true` from the PWA (otherwise the capture is queued and you must poll `GET /v1/captures/{id}`).

### 6.3 CaptureSplit 🆕 (one per person page touched)
```json
{
  "personId": "…", "personName": "妈妈", "tier": "gold", "isNewPerson": false,
  "materialId": "<recordId>", "tag": "回家吃饭", "dur": "0:07", "body": "5月12日回家吃饭",
  "presentation": "pencil",                    // 🆕 ink | pencil | question
  "glanceHint": "5月12日 · 点一下确认日期",       // 🆕 short Chinese hint or null
  "factIds": ["<factId>"],                      // 🆕 facts written from this split
  "inboxItemId": null                           // 🆕 open review item id (pencil-pending / question), else null
}
```

### 6.4 Undo (5 s)
```http
POST /v1/captures/{captureId}:undo   (Idempotency-Key)
→ 200 {"captureId","status":"reverted","revertedRecordCount":1,"rejectedFactCount":2,"cancelledReminderCount":1}
```
Use `jobId` from voice ingest as `captureId`. After `undoDeadline` → `409`.

## 7. Person page materials 🆕 pencil drafts (D1)

```http
GET /v1/persons/{personId}/materials?limit=20&cursor=…&includeHeld=false
→ 200 {"items": [MaterialDto…], "nextCursor": null, "hasMore": false}
```
```json
MaterialDto {
  "id": "<recordId>", "personId", "captureId", "kind": "voice", "type": "voice",
  "title": "回家吃饭", "body": "5月12日回家吃饭", "snippet": "…",
  "hasMedia": true, "mediaUrl": "/v1/materials/<id>/media?uid=…&exp=…&sig=…", "dur": "0:07", "audioDurationMs": 7000,
  "stampCode": null, "happenedOn": "2026-11-16", "pinned": true,
  "reviewState": "normal",                    // normal | held | note_only
  "presentation": "pencil",                   // 🆕 ink | pencil | question
  "glanceHint": "5月12日 · 点一下确认日期",      // 🆕
  "pencilReason": "date_glance",              // 🆕 date_glance | low_confidence | speaker_unknown | polarity_flip | multi_person | venting | new_person | ambiguous_person | null
  "inboxItemId": null,                        // 🆕 set for pending pencil / question / venting
  "autoInkAt": null,                          // 🆕 when a pending pencil auto-turns to ink (48h; never for venting)
  "pencilFacts": [ FactDto… ],                // 🆕 only the facts that still need a look (with triggerDate for the date capsule)
  "version": 1, "createdAt", "updatedAt"
}
```
`FactDto` gains 🆕 `"needsGlance": true|false`.

**Rendering rules**
| presentation | pencilReason | Look | User action → API |
|---|---|---|---|
| `ink` | null | normal | – |
| `pencil` | `date_glance` | dashed pencil edge, date capsule "5月12日 ✎" (fact is already confirmed, reminder already active) | ✔ looks right: `PATCH /v1/facts/{factId}` `{"status":"confirmed"}` · ✎ change date: `PATCH /v1/facts/{factId}` `{"triggerDate":"2027-05-12"}` → both clear `needsGlance`; material turns ink |
| `pencil` | `low_confidence` / `speaker_unknown` / `polarity_flip` / `multi_person` | pencil; auto-inks at `autoInkAt` | confirm: `POST /v1/inbox/{inboxItemId}:resolve` `{"action":"confirm"}` (optionally `"triggerDate"`) · drop: `{"action":"discard"}` · keep as plain note: `{"action":"keep_as_note"}` |
| `pencil` | `venting` | only returned with `includeHeld=true`; render in a private "冷静区" | `:resolve` `{"action":"confirm"}` / `{"action":"discard"}` |
| `question` | `new_person` / `ambiguous_person` | question sticky note, up to 3 candidate avatars (`GET /v1/inbox?personId=` → `candidatePersons`) | `:resolve` `{"action":"assign_person","targetPersonId":"…"}` / `{"action":"create_person","displayName":"…","tier":"crystal"}` / `{"action":"confirm"}` (accept new person) |

All of these require `Idempotency-Key`.

## 8. Cupid chat – SSE

```http
POST /v1/persons/{personId}/chat/stream   (Bearer, Idempotency-Key, Content-Type: application/json)
{"message": "她最近怎么了", "clientMessageId": "<uuid>", "referencedMaterialIds": ["<recordId>"]}
→ 200 text/event-stream
```
It is a **POST** → use `fetch()` + `ReadableStream` and parse `event:`/`data:` frames (not `EventSource`).

| event | data (JSON) | Notes |
|---|---|---|
| `delta` | `{"text": "…"}` | append to the bubble (citation markers already stripped) |
| `citation` | `{"index": 1, "materialId": "<recordId>", "title": "…", "snippet": "…", "happenedOn": "2026-05-12"}` | render as a mini polaroid / voice chip – never the id |
| `whisper` | `{"text": "…"}` | small aside note |
| `perspective` | `{"items": ["…"]}` | gold tier only |
| `suggestion` | `{"items": ["…"]}` | tappable chips |
| `crisis` | CrisisCard `{"templateId","title","careMessage","hotlines":[{"name","phone"}],"disclaimer"}` | replace the whole reply with the card |
| `error` | `{"code": "ai_upstream_error", "detail": "…"}` | show a soft retry; same Idempotency-Key is safe to retry |
| `done` | `{"messageId","tierSnapshot","hasUnrecordedGap","citedMaterialIds":[…]}` | always last on success |

History: `GET /v1/persons/{id}/chat/messages?order=asc&limit=20&cursor=…` → `{"items":[ChatMessageDto…],"nextCursor","hasMore"}`.

## 9. Preferences (paper texture etc.)

```http
GET /v1/me/preferences → PreferencesResponse (see bootstrap.preferences, without "edition")
PATCH /v1/me/preferences   (Idempotency-Key, If-Match optional)
{"preferredEdition": "parchment", "wallpaperPreset": "warm_paper", "wallpaperBlur": 8}
```
| field | values |
|---|---|
| `preferredEdition` (paper texture) | `cotton` · `dotgrid` · `parchment` · `traveler` · `hobonichi` · `museglass` |
| `wallpaperPreset` | free key 1..48 printable chars (default `warm_paper`) |
| `wallpaperBlur` / `wallpaperVignette` | ints (see OpenAPI bounds) |
| `cupidLayout` | `a`·`b`·`c`·`d` |
| `appLockEnabled`, `lateNightCareEnabled` | bool |
| `timezone` | IANA name |

## 10. Changelog
- v0 – initial contract (existing endpoints + planned pencil fields).
