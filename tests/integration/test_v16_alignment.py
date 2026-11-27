"""Live PostgreSQL integration tests for MindWeave v16 backend synchronization & alignment.

Covers:
1. Decomposed WeChat archive cards (`RecordKind.CHAT`) via `/v1/captures` with AES-256-GCM
   AAD-bound `card_meta_enc` ciphertext verification (zero plaintext PII at rest).
2. `isFavorite` toggle + `cardMeta` patch via `PATCH /v1/materials/{id}` with optimistic locking
   (`If-Match`), idempotency replay, and Invariant I6 forbidden-copy rejection.
3. One-tap photo-to-background promotion (`POST /v1/persons/{id}/background:from-material`)
   with real-photo chroma + faceZone extraction AND strict 422 Silver Shield privacy guard.
4. Enriched single-round-trip `GET /v1/bootstrap` (`facts[]`, `upcomingReminders[]`,
   `coolingCount`, `materials[].isFavorite`, `materials[].cardMeta`) excluding held/deleted rows,
   plus `Edition.LUMINA` preference round-trip.
5. GB 45438-2025 PIPL data export (`POST /v1/me/exports`) preserving decrypted `isFavorite`
   and `cardMeta` on surviving non-held records.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from PIL import Image, ImageDraw
from sqlalchemy import select

from mindweave.core.clock import ManualClock
from mindweave.core.crypto import CryptoError
from mindweave.core.dependencies import AppContainer
from mindweave.core.ids import new_id
from mindweave.core.orm import InboxItemRow, RecordRow, UserKeyRow, UserRow
from mindweave.main import create_app
from mindweave.shared.scopes import UserScope


@pytest.fixture(autouse=True)
def _anchor_clock_in_past(manual_clock: ManualClock) -> None:
    """Keep ManualClock safely behind wall-clock so PyJWT `iat` checks always pass."""
    wall_now = datetime.now(UTC)
    if manual_clock.now() > wall_now - timedelta(minutes=2):
        manual_clock.set(wall_now - timedelta(minutes=5))


async def _provision_user(container: AppContainer) -> tuple[UUID, UserScope, str, bytes]:
    user_id = new_id()
    scope = UserScope(user_id=user_id)
    wrapped_dek, kek_id = container.cipher.generate_wrapped_dek(user_id)
    dek = container.cipher.load_dek(user_id=user_id, wrapped_dek=wrapped_dek, kek_id=kek_id)

    async with container.db.system_session() as sys_sess:
        sys_sess.add(UserRow(id=user_id, status="active"))
    async with container.db.user_session(scope) as sess:
        sess.add(UserKeyRow(user_id=user_id, wrapped_dek=wrapped_dek, kek_id=kek_id))

    token, _ = container.tokens.create_access_token(user_id, now=container.clock.now())
    return user_id, scope, token, dek


def _headers(
    token: str,
    *,
    idem: str | None = None,
    if_match: int | None = None,
    reauth: str | None = None,
) -> dict[str, str]:
    h = {"Authorization": f"Bearer {token}"}
    if idem is not None:
        h["Idempotency-Key"] = idem
    if if_match is not None:
        h["If-Match"] = f'"{if_match}"'
    if reauth is not None:
        h["X-Reauth-Token"] = reauth
    return h


def _make_warm_portrait_jpeg_with_exif() -> bytes:
    """Create a rich multi-zone warm portrait image with EXIF tags for chroma + EXIF testing."""
    img = Image.new("RGB", (160, 120), color=(32, 54, 40))  # deep garden foliage left
    draw = ImageDraw.Draw(img)
    # Warm sunlit skin-like ellipse on upper-right zone (tests faceZone + warm pigment)
    draw.ellipse((98, 12, 144, 62), fill=(216, 166, 134))
    # Bright sunlight rim top-left
    draw.rectangle((4, 4, 46, 36), fill=(236, 224, 198))
    # Warm terracotta accent lower-right
    draw.rectangle((90, 74, 152, 114), fill=(198, 126, 78))

    exif = Image.Exif()
    exif[0x010F] = "Leica"
    exif[0x013B] = "PrivatePhotographer"
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92, exif=exif.tobytes())
    return buf.getvalue()


@pytest.mark.asyncio
async def test_v16_chat_capture_decomposed_card_and_aad_ciphertext_zero_leak(
    db_container: AppContainer,
) -> None:
    user_id, scope, token, dek = await _provision_user(db_container)
    app = create_app(container=db_container)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        person_resp = await client.post(
            "/v1/persons",
            headers=_headers(token, idem="v16-create-mom"),
            json={"nameCn": "妈妈", "roleTag": "母亲", "tier": "gold"},
        )
        assert person_resp.status_code == 201, person_resp.text
        mom_id = person_resp.json()["id"]

        chat_text = (
            "妈妈：这周冷空气下来了，骑车记得穿厚外套\n"
            "我：穿啦穿啦，周六接你去郊外摘豌豆苗记得带保温杯\n"
            "妈妈：哈哈那行吧，我明早把保温杯备好"
        )
        cap_resp = await client.post(
            "/v1/captures?sync=true",
            headers=_headers(token, idem="v16-chat-cap"),
            json={
                "source": "chat",
                "routeMode": "current",
                "activePersonId": mom_id,
                "text": chat_text,
            },
        )
        assert cap_resp.status_code == 201, cap_resp.text
        cap_data = cap_resp.json()
        assert len(cap_data["splits"]) >= 1
        split0 = cap_data["splits"][0]
        assert split0["kind"] == "chat"
        mat_id = UUID(split0["materialId"])

        mat_resp = await client.get(f"/v1/materials/{mat_id}", headers=_headers(token))
        assert mat_resp.status_code == 200, mat_resp.text
        mat_data = mat_resp.json()
        assert mat_data["kind"] == "chat"
        assert mat_data["isFavorite"] is False
        assert isinstance(mat_data["cardMeta"], dict)
        messages = mat_data["cardMeta"].get("messages")
        assert isinstance(messages, list) and len(messages) == 3
        assert messages[0]["from"] == "妈妈"
        assert messages[0]["sender"] == "them"
        assert messages[1]["from"] == "我"
        assert messages[1]["sender"] == "me"
        assert "豌豆苗" in messages[1]["text"]

    # Verify raw database ciphertext contains ZERO plaintext PII & enforces AAD binding
    async with db_container.db.user_session(scope) as sess:
        row = await sess.scalar(select(RecordRow).where(RecordRow.id == mat_id))
        assert row is not None
        assert row.card_meta_enc is not None
        assert "豌豆苗".encode() not in row.card_meta_enc
        assert "保温杯".encode() not in row.card_meta_enc
        with pytest.raises(CryptoError):
            db_container.cipher.decrypt_json(
                dek,
                row.card_meta_enc,
                user_id=user_id,
                table="records",
                column="wrong_col",
                row_id=mat_id,
            )


@pytest.mark.asyncio
async def test_v16_material_favorite_and_card_meta_patch_with_optimistic_lock_and_idempotency(
    db_container: AppContainer,
) -> None:
    _, _, token, _ = await _provision_user(db_container)
    app = create_app(container=db_container)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        mom_id = (
            await client.post(
                "/v1/persons",
                headers=_headers(token, idem="v16-fav-mom"),
                json={"nameCn": "妈妈", "tier": "gold"},
            )
        ).json()["id"]

        created = await client.post(
            f"/v1/persons/{mom_id}/materials",
            headers=_headers(token, idem="v16-draft-create"),
            json={
                "kind": "draft",
                "title": "待核对农历护膝日期",
                "body": "农历冬月初八记得提前买羊绒护膝。",
                "isFavorite": False,
                "cardMeta": {
                    "lunarLabel": "农历冬月初八",
                    "lunarHint": "12月16日 ✎ · 轻点核对",
                },
            },
        )
        assert created.status_code == 201, created.text
        mat = created.json()
        mat_id = mat["id"]
        assert mat["kind"] == "draft"
        assert mat["isFavorite"] is False
        assert mat["cardMeta"]["lunarLabel"] == "农历冬月初八"
        assert mat["version"] == 1

        # Null byte inside cardMeta MUST be rejected with 422
        null_meta = await client.patch(
            f"/v1/materials/{mat_id}",
            headers=_headers(token, idem="v16-null-meta", if_match=1),
            json={"cardMeta": {"note": "bad\x00value"}},
        )
        assert null_meta.status_code == 422

        # Patch favorite + confirmed lunar hint + verify forbidden dev jargon stripped (Invariant I6)
        patch_payload = {
            "isFavorite": True,
            "cardMeta": {
                "lunarLabel": "农历冬月初八",
                "lunarHint": "✓ 已落墨 2026·12·16（提前 1 天）",
                "note": "系统自动归档 护膝备忘",
            },
        }
        patched = await client.patch(
            f"/v1/materials/{mat_id}",
            headers=_headers(token, idem="v16-patch-fav", if_match=1),
            json=patch_payload,
        )
        assert patched.status_code == 200, patched.text
        p_body = patched.json()
        assert p_body["isFavorite"] is True
        assert p_body["version"] == 2
        assert p_body["cardMeta"]["lunarHint"].startswith("✓ 已落墨")
        assert "系统自动归档" not in p_body["cardMeta"]["note"]

        # Idempotent replay returns identical result
        replayed = await client.patch(
            f"/v1/materials/{mat_id}",
            headers=_headers(token, idem="v16-patch-fav", if_match=1),
            json=patch_payload,
        )
        assert replayed.status_code == 200
        assert replayed.json()["version"] == 2
        assert replayed.json()["isFavorite"] is True

        # Stale If-Match -> 412 Precondition Failed
        stale = await client.patch(
            f"/v1/materials/{mat_id}",
            headers=_headers(token, idem="v16-patch-stale", if_match=1),
            json={"isFavorite": False},
        )
        assert stale.status_code == 412


@pytest.mark.asyncio
async def test_v16_promote_photo_material_to_background_extracts_chroma_and_guards_silver_shield(
    db_container: AppContainer,
) -> None:
    _, _, token, _ = await _provision_user(db_container)
    app = create_app(container=db_container)
    jpeg_bytes = _make_warm_portrait_jpeg_with_exif()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        # 1. Gold person: upload photo material & promote to hero background
        mom_id = (
            await client.post(
                "/v1/persons",
                headers=_headers(token, idem="v16-bg-mom"),
                json={"nameCn": "妈妈", "tier": "gold"},
            )
        ).json()["id"]

        photo_mat = await client.post(
            f"/v1/persons/{mom_id}/materials",
            headers=_headers(token, idem="v16-photo-mat"),
            data={
                "kind": "photo",
                "title": "花园暖笑",
                "body": "周五午后去菜地挑嫩豌豆苗。",
                "isFavorite": "true",
                "cardMeta": json.dumps({"sceneLabel": "周五午后 · 乡下菜地"}),
            },
            files={"photo": ("garden.jpg", jpeg_bytes, "image/jpeg")},
        )
        assert photo_mat.status_code == 201, photo_mat.text
        pm = photo_mat.json()
        assert pm["kind"] == "photo"
        assert pm["hasMedia"] is True
        assert pm["isFavorite"] is True
        assert isinstance(pm["cardMeta"].get("palette"), list)
        assert len(pm["cardMeta"]["palette"]) == 3

        promo = await client.post(
            f"/v1/persons/{mom_id}/background:from-material",
            headers=_headers(token, idem="v16-promote-bg"),
            json={"materialId": pm["id"]},
        )
        assert promo.status_code == 200, promo.text
        p_dto = promo.json()
        vt = p_dto["visualTokens"]
        assert vt["activeHeroMaterialId"] == pm["id"]
        assert vt["realWarm"].startswith("#")
        assert vt["realCool"].startswith("#")
        assert vt["realLight"].startswith("#")
        assert vt["realVivid"].startswith("#")
        assert vt["faceZone"] in ("left", "center", "right")
        assert "deskFocus" in vt and "phoneFocus" in vt
        assert p_dto["mainPhotoUrl"] is not None

        # Verify served main photo has EXIF stripped
        photo_resp = await client.get(p_dto["mainPhotoUrl"])
        assert photo_resp.status_code == 200
        with Image.open(io.BytesIO(photo_resp.content)) as served_img:
            assert len(served_img.getexif()) == 0

        # Idempotent replay of promotion succeeds
        promo_replay = await client.post(
            f"/v1/persons/{mom_id}/background:from-material",
            headers=_headers(token, idem="v16-promote-bg"),
            json={"materialId": pm["id"]},
        )
        assert promo_replay.status_code == 200
        assert promo_replay.json()["visualTokens"]["activeHeroMaterialId"] == pm["id"]

        # 2. Silver Shield person: promoting background MUST return 422
        silver_id = (
            await client.post(
                "/v1/persons",
                headers=_headers(token, idem="v16-bg-silver"),
                json={"nameCn": "陆主管", "roleTag": "跨组对接", "tier": "silver"},
            )
        ).json()["id"]

        silver_photo = await client.post(
            f"/v1/persons/{silver_id}/materials",
            headers=_headers(token, idem="v16-silver-mat"),
            data={"kind": "photo", "title": "留痕附件", "body": "书面确认邮件留痕。"},
            files={"photo": ("proof.jpg", jpeg_bytes, "image/jpeg")},
        )
        assert silver_photo.status_code == 201

        silver_promo = await client.post(
            f"/v1/persons/{silver_id}/background:from-material",
            headers=_headers(token, idem="v16-silver-promote"),
            json={"materialId": silver_photo.json()["id"]},
        )
        assert silver_promo.status_code == 422


@pytest.mark.asyncio
async def test_v16_bootstrap_enriched_facts_reminders_cooling_and_lumina_preference(
    db_container: AppContainer,
) -> None:
    user_id, scope, token, dek = await _provision_user(db_container)
    app = create_app(container=db_container)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        # Auto-provision preferences via initial bootstrap, then switch to Edition.LUMINA
        b0 = await client.get("/v1/bootstrap", headers=_headers(token))
        assert b0.status_code == 200
        assert b0.json()["preferences"]["preferredEdition"] == "cotton"

        pref_patch = await client.patch(
            "/v1/me/preferences",
            headers=_headers(token, idem="v16-pref-lumina", if_match=1),
            json={"preferredEdition": "lumina"},
        )
        assert pref_patch.status_code == 200, pref_patch.text
        assert pref_patch.json()["preferredEdition"] == "lumina"

        # Seed Gold person with facts, reminder, favorite material, held material & cooling inbox
        mom_id = (
            await client.post(
                "/v1/persons",
                headers=_headers(token, idem="v16-boot-mom"),
                json={"nameCn": "妈妈", "roleTag": "母亲", "tier": "gold"},
            )
        ).json()["id"]

        f_conf = await client.post(
            f"/v1/persons/{mom_id}/facts",
            headers=_headers(token, idem="v16-fact-conf"),
            json={
                "type": "preference",
                "polarity": "positive",
                "content": "嫩豌豆苗清汤 · 午后清茶",
            },
        )
        assert f_conf.status_code == 201, f_conf.text

        f_rej_create = await client.post(
            f"/v1/persons/{mom_id}/facts",
            headers=_headers(token, idem="v16-fact-rej"),
            json={
                "type": "taboo",
                "polarity": "negative",
                "content": "已淡忘的旧习惯",
            },
        )
        assert f_rej_create.status_code == 201, f_rej_create.text
        f_rej_id = f_rej_create.json()["id"]
        f_rej_patch = await client.patch(
            f"/v1/facts/{f_rej_id}",
            headers=_headers(token, idem="v16-fact-rej-patch", if_match=1),
            json={"status": "rejected"},
        )
        assert f_rej_patch.status_code == 200, f_rej_patch.text

        rem_date = date.today() + timedelta(days=5)
        rem = await client.post(
            "/v1/reminders",
            headers=_headers(token, idem="v16-rem-1"),
            json={
                "personId": mom_id,
                "title": "周六带保温杯接妈妈郊外挑豌豆苗",
                "rule": "once",
                "onceDate": rem_date.isoformat(),
                "leadDays": [1, 0],
            },
        )
        assert rem.status_code == 201, rem.text

        mat_vis = await client.post(
            f"/v1/persons/{mom_id}/materials",
            headers=_headers(token, idem="v16-mat-vis"),
            json={
                "kind": "voice",
                "title": "动态血糖仪摸清控糖雷区",
                "body": "原味奶茶和芋泥点心一吃血糖就高。",
                "isFavorite": True,
                "cardMeta": {"duration": 18, "segments": ["原味奶茶和芋泥点心一吃血糖就高。"]},
            },
        )
        assert mat_vis.status_code == 201

        # Seed a held venting record & pending cooling inbox item directly in PostgreSQL
        now = db_container.clock.now()
        held_rec_id = new_id()
        cooling_id = new_id()
        async with db_container.db.user_session(scope) as sess:
            sess.add(
                RecordRow(
                    id=held_rec_id,
                    user_id=user_id,
                    person_id=UUID(mom_id),
                    kind="note",
                    title_enc=db_container.cipher.encrypt_str(
                        dek,
                        "气话暂存",
                        user_id=user_id,
                        table="records",
                        column="title_enc",
                        row_id=held_rec_id,
                    ),
                    body_enc=db_container.cipher.encrypt_str(
                        dek,
                        "刚才气头上说的话，暂存冷静区。",
                        user_id=user_id,
                        table="records",
                        column="body_enc",
                        row_id=held_rec_id,
                    ),
                    review_state="held",
                    created_at=now,
                    updated_at=now,
                )
            )
            sess.add(
                InboxItemRow(
                    id=cooling_id,
                    user_id=user_id,
                    person_id=UUID(mom_id),
                    kind="venting_hold",
                    status="open",
                    payload_enc=db_container.cipher.encrypt_json(
                        dek,
                        {"text": "刚才气头上想说的话"},
                        user_id=user_id,
                        table="inbox_items",
                        column="payload_enc",
                        row_id=cooling_id,
                    ),
                    expires_at=now + timedelta(hours=72),
                    created_at=now,
                )
            )

        b1 = await client.get("/v1/bootstrap", headers=_headers(token))
        assert b1.status_code == 200, b1.text
        snap = b1.json()
        assert snap["preferences"]["preferredEdition"] == "lumina"
        assert snap["preferences"]["edition"] == "lumina"
        assert snap["inboxCount"] == 1

        p_entry = snap["people"][mom_id]
        assert p_entry["coolingCount"] == 1
        fact_contents = [f["content"] for f in p_entry["facts"]]
        assert "嫩豌豆苗清汤 · 午后清茶" in fact_contents
        assert "已淡忘的旧习惯" not in fact_contents

        assert len(p_entry["upcomingReminders"]) == 1
        assert p_entry["upcomingReminders"][0]["title"] == "周六带保温杯接妈妈郊外挑豌豆苗"

        mat_ids = [m["id"] for m in p_entry["materials"]]
        assert mat_vis.json()["id"] in mat_ids
        assert str(held_rec_id) not in mat_ids
        vis_entry = next(m for m in p_entry["materials"] if m["id"] == mat_vis.json()["id"])
        assert vis_entry["isFavorite"] is True
        assert vis_entry["cardMeta"]["duration"] == 18
        assert vis_entry["version"] == 1


@pytest.mark.asyncio
async def test_v16_pipl_export_includes_decrypted_favorite_and_card_meta(
    db_container: AppContainer,
) -> None:
    user_id, _, token, _ = await _provision_user(db_container)
    reauth_token, _ = db_container.tokens.create_reauth_token(
        user_id,
        now=db_container.clock.now(),
    )
    app = create_app(container=db_container)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        mom_id = (
            await client.post(
                "/v1/persons",
                headers=_headers(token, idem="v16-exp-mom"),
                json={"nameCn": "妈妈", "tier": "gold"},
            )
        ).json()["id"]

        mat = await client.post(
            f"/v1/persons/{mom_id}/materials",
            headers=_headers(token, idem="v16-exp-chat"),
            json={
                "kind": "chat",
                "title": "哄妈妈出门的小法子",
                "body": "请她帮忙挑嫩豌豆苗尖尖。",
                "isFavorite": True,
                "cardMeta": {
                    "messages": [{"from": "妈妈", "sender": "them", "text": "明天我备好保温杯"}],
                    "summary": "给一个需要她帮忙的小理由",
                },
            },
        )
        assert mat.status_code == 201

        exp_resp = await client.post(
            "/v1/me/exports",
            headers=_headers(token, idem="v16-export-1", reauth=reauth_token),
            json={"asyncMode": False},
        )
        assert exp_resp.status_code == 201, exp_resp.text
        download_path = exp_resp.json()["downloadPath"]
        assert download_path is not None

        dl = await client.get(download_path, headers=_headers(token))
        assert dl.status_code == 200
        with zipfile.ZipFile(io.BytesIO(dl.content), "r") as zf:
            exported = json.loads(zf.read("data.json").decode("utf-8"))

        exported_materials = exported["materials"]
        assert len(exported_materials) == 1
        rec0 = exported_materials[0]
        assert rec0["kind"] == "chat"
        assert rec0["isFavorite"] is True
        assert rec0["cardMeta"]["summary"] == "给一个需要她帮忙的小理由"
        assert rec0["cardMeta"]["messages"][0]["text"] == "明天我备好保温杯"
