"""Live PostgreSQL integration tests for People REST API, EntityResolver, and V10 Bootstrap."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image
from sqlalchemy import func, select

from mindweave.core.clock import ManualClock
from mindweave.core.crypto import CryptoError, normalize_alias_for_index
from mindweave.core.dependencies import AppContainer
from mindweave.core.errors import register_exception_handlers
from mindweave.core.ids import new_id
from mindweave.core.orm import (
    ChatMessageRow,
    ChatThreadRow,
    FactRow,
    InboxItemRow,
    PersonAliasRow,
    PersonRow,
    RecordRow,
    ReminderRow,
    UserKeyRow,
    UserPreferenceRow,
    UserRow,
)
from mindweave.modules.bootstrap.api.router import router as bootstrap_router
from mindweave.modules.people.api.router import router as people_router
from mindweave.modules.people.application.entity_resolver import EntityResolver
from mindweave.shared.enums import UNASSIGNED_PERSON_SLUG, PersonState, RouteMode, Tier
from mindweave.shared.scopes import UserScope


def _build_test_app(container: AppContainer) -> FastAPI:
    app = FastAPI()
    app.state.container = container
    register_exception_handlers(app)
    app.include_router(people_router, prefix="/v1")
    app.include_router(bootstrap_router, prefix="/v1")
    return app


async def _provision_user(container: AppContainer) -> tuple[UUID, UserScope, str, bytes]:
    wall_now = datetime.now(UTC)
    if isinstance(container.clock, ManualClock) and container.clock.now() > wall_now:
        container.clock.set(wall_now - timedelta(minutes=5))

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


def _auth_headers(
    token: str, *, idem_key: str | None = None, if_match: int | None = None
) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if idem_key is not None:
        headers["Idempotency-Key"] = idem_key
    if if_match is not None:
        headers["If-Match"] = str(if_match)
    return headers


def _make_jpeg_with_exif() -> bytes:
    img = Image.new("RGB", (64, 48), color=(212, 154, 69))
    exif = Image.Exif()
    exif[0x010F] = "Canon"  # Make
    exif[0x013B] = "SecretAuthorName"  # Artist
    exif[0x0131] = "CameraGPSFirmware"  # Software
    buf = BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes())
    raw = buf.getvalue()
    # Sanity check that source image truly carries EXIF metadata before upload
    with Image.open(BytesIO(raw)) as check_img:
        assert len(check_img.getexif()) >= 3
    return raw


@pytest.mark.asyncio
async def test_bootstrap_brand_new_user_auto_provisions_preferences_and_empty_state(
    db_container: AppContainer,
) -> None:
    user_id, scope, token, _ = await _provision_user(db_container)
    app = _build_test_app(db_container)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.get("/v1/bootstrap", headers=_auth_headers(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["coreIds"] == []
        assert data["casualIds"] == []
        assert data["people"] == {}
        assert data["inboxCount"] == 0
        assert data["preferences"]["edition"] == "cotton"
        assert data["preferences"]["preferredEdition"] == "cotton"
        assert data["preferences"]["cupidLayout"] == "a"
        assert data["preferences"]["hasCustomWallpaper"] is False
        assert data["preferences"]["version"] == 1

    async with db_container.db.user_session(scope) as sess:
        pref_row = await sess.get(UserPreferenceRow, user_id)
        assert pref_row is not None
        assert pref_row.preferred_edition == "cotton"


@pytest.mark.asyncio
async def test_person_crud_aad_encryption_idempotency_and_optimistic_locking(
    db_container: AppContainer,
) -> None:
    user_id, scope, token, dek = await _provision_user(db_container)
    app = _build_test_app(db_container)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        # Missing Idempotency-Key -> 400
        no_idem = await client.post(
            "/v1/persons",
            headers=_auth_headers(token),
            json={"nameCn": "沈清秋", "tier": "love"},
        )
        assert no_idem.status_code == 400

        # Forbidden UI dev phrase (Invariant I6) -> 422
        bad_copy = await client.post(
            "/v1/persons",
            headers=_auth_headers(token, idem_key="idem-bad-copy"),
            json={"nameCn": "沈清秋", "shortMemo": "系统自动归档 · 海风", "tier": "gold"},
        )
        assert bad_copy.status_code == 422

        # Create person accepting legacy 'love' -> canonical 'gold', Zero-Photo Burden defaults
        create_payload = {
            "nameCn": "沈清秋",
            "roleTag": "心动伴侣",
            "contextSub": "四月海风 · 镰仓木栈道",
            "shortMemo": "偏爱温热桂花燕麦拿铁，生理期前三天忌冰美式。",
            "traits": ["每年四月都想看一次海", "偏爱拍立得相纸质感"],
            "tier": "love",
            "aliases": ["清秋", {"alias": "小沈", "kind": "nickname"}],
        }
        created = await client.post(
            "/v1/persons",
            headers=_auth_headers(token, idem_key="idem-create-chloe"),
            json=create_payload,
        )
        assert created.status_code == 201
        chloe = created.json()
        chloe_id = UUID(chloe["id"])
        assert chloe["tier"] == "gold"
        assert chloe["monogram"] == "沈"
        assert chloe["cupidTitle"] == "沈清秋的丘比特"
        assert chloe["hasPhoto"] is False
        assert chloe["avatarUrl"] is None
        assert chloe["mainPhotoUrl"] is None
        assert chloe["subPhotoUrl"] is None
        assert chloe["version"] == 1

        # Exact replay with same Idempotency-Key returns cached 201 without creating a second row
        replayed = await client.post(
            "/v1/persons",
            headers=_auth_headers(token, idem_key="idem-create-chloe"),
            json=create_payload,
        )
        assert replayed.status_code == 201
        assert replayed.json()["id"] == chloe["id"]

        # Reusing same Idempotency-Key with different payload -> 409 IdempotencyConflictError
        conflict = await client.post(
            "/v1/persons",
            headers=_auth_headers(token, idem_key="idem-create-chloe"),
            json={**create_payload, "nameCn": "周叙白"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "idempotency_conflict"

        # Verify raw DB ciphertext & strict per-column AAD binding (Invariant I12)
        async with db_container.db.user_session(scope) as sess:
            row = await sess.get(PersonRow, chloe_id)
            assert row is not None
            assert b"\xe6\xb2\x88" not in row.display_name_enc
            assert (
                db_container.cipher.decrypt_str(
                    dek,
                    row.display_name_enc,
                    user_id=user_id,
                    table="persons",
                    column="display_name_enc",
                    row_id=chloe_id,
                )
                == "沈清秋"
            )
            assert db_container.cipher.decrypt_json(
                dek,
                row.traits_enc or b"",
                user_id=user_id,
                table="persons",
                column="traits_enc",
                row_id=chloe_id,
            ) == ["每年四月都想看一次海", "偏爱拍立得相纸质感"]
            with pytest.raises(CryptoError):
                db_container.cipher.decrypt_str(
                    dek,
                    row.display_name_enc,
                    user_id=user_id,
                    table="persons",
                    column="role_tag_enc",
                    row_id=chloe_id,
                )

        # PATCH with If-Match: 1 updates nameCn + syncs primary alias -> version 2
        patched = await client.patch(
            f"/v1/persons/{chloe_id}",
            headers=_auth_headers(token, idem_key="idem-patch-1", if_match=1),
            json={"nameCn": "沈清秋Chloé", "shortMemo": "更新后的手札摘要"},
        )
        assert patched.status_code == 200
        assert patched.json()["version"] == 2
        assert patched.json()["cupidTitle"] == "沈清秋Chloé的丘比特"

        # Stale If-Match: 1 -> 412 VersionMismatchError
        stale = await client.patch(
            f"/v1/persons/{chloe_id}",
            headers=_auth_headers(token, idem_key="idem-patch-stale", if_match=1),
            json={"shortMemo": "并发过期写入"},
        )
        assert stale.status_code == 412
        assert stale.json()["code"] == "version_mismatch"

        # Verify primary alias was updated to new nameCn
        aliases_resp = await client.get(
            f"/v1/persons/{chloe_id}/aliases", headers=_auth_headers(token)
        )
        assert aliases_resp.status_code == 200
        alias_items = aliases_resp.json()["items"]
        primary = next(a for a in alias_items if a["isPrimary"])
        assert primary["alias"] == "沈清秋Chloé"
        assert primary["kind"] == "name"

        # Seed an active reminder on Chloe + internal __unassigned__ placeholder row
        arch_rem_id = new_id()
        unassigned_id = new_id()
        async with db_container.db.user_session(scope) as sess:
            sess.add(
                ReminderRow(
                    id=arch_rem_id,
                    user_id=user_id,
                    person_id=chloe_id,
                    title_enc=db_container.cipher.encrypt_str(
                        dek,
                        "海边纪念日",
                        user_id=user_id,
                        table="reminders",
                        column="title_enc",
                        row_id=arch_rem_id,
                    ),
                    status="active",
                    next_fire_at=db_container.clock.now() + timedelta(days=1),
                )
            )
            sess.add(
                PersonRow(
                    id=unassigned_id,
                    user_id=user_id,
                    slug=UNASSIGNED_PERSON_SLUG,
                    tier="crystal",
                    rank="zzzz",
                    state="provisional",
                    display_name_enc=db_container.cipher.encrypt_str(
                        dek,
                        "待归档",
                        user_id=user_id,
                        table="persons",
                        column="display_name_enc",
                        row_id=unassigned_id,
                    ),
                    monogram="待",
                )
            )

        # Archive & unarchive lifecycle (archiving pauses active reminders & hides __unassigned__)
        archived = await client.patch(
            f"/v1/persons/{chloe_id}",
            headers=_auth_headers(token, idem_key="idem-archive", if_match=2),
            json={"archived": True},
        )
        assert archived.status_code == 200
        assert archived.json()["state"] == "archived"

        async with db_container.db.user_session(scope) as sess:
            paused_rem = await sess.get(ReminderRow, arch_rem_id)
            assert paused_rem is not None
            assert paused_rem.status == "paused"

        default_list = await client.get("/v1/persons", headers=_auth_headers(token))
        assert default_list.json()["coreIds"] == []
        assert default_list.json()["casualIds"] == []
        assert default_list.json()["items"] == []
        assert (
            await client.get(f"/v1/persons/{unassigned_id}", headers=_auth_headers(token))
        ).status_code == 404
        assert (await client.get("/v1/bootstrap", headers=_auth_headers(token))).json()[
            "people"
        ] == {}

        with_arch = await client.get(
            "/v1/persons?includeArchived=true", headers=_auth_headers(token)
        )
        assert len(with_arch.json()["items"]) == 1
        assert with_arch.json()["coreIds"] == []

        with_arch_snake = await client.get(
            "/v1/persons?include_archived=true", headers=_auth_headers(token)
        )
        assert len(with_arch_snake.json()["items"]) == 1

        unarchived = await client.patch(
            f"/v1/persons/{chloe_id}",
            headers=_auth_headers(token, idem_key="idem-unarchive", if_match=3),
            json={"archived": False},
        )
        assert unarchived.status_code == 200
        assert unarchived.json()["state"] == "active"
        assert (await client.get("/v1/persons", headers=_auth_headers(token))).json()[
            "coreIds"
        ] == [str(chloe_id)]


@pytest.mark.asyncio
async def test_tier_transitions_move_concurrency_and_zero_history_loss(
    db_container: AppContainer,
) -> None:
    user_id, scope, token, dek = await _provision_user(db_container)
    app = _build_test_app(db_container)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        ids: dict[str, str] = {}
        for key, name_cn, tier in [
            ("chloe", "沈清秋", "gold"),
            ("xubai", "周叙白", "gold"),
            ("zhixia", "林知夏", "silver"),
            ("prof", "陈教授", "crystal"),
            ("kevin", "Kevin", "crystal"),
        ]:
            r = await client.post(
                "/v1/persons",
                headers=_auth_headers(token, idem_key=f"seed-{key}"),
                json={"nameCn": name_cn, "tier": tier},
            )
            assert r.status_code == 201
            ids[key] = r.json()["id"]

        assert (
            await client.get(f"/v1/persons/{ids['kevin']}", headers=_auth_headers(token))
        ).json()["monogram"] == "K"

        # Seed RecordRow, FactRow, ReminderRow, and ChatThreadRow + ChatMessageRow on Chloe
        chloe_uuid = UUID(ids["chloe"])
        rec_id, fact_id, rem_id, thread_id, msg_id = (
            new_id(),
            new_id(),
            new_id(),
            new_id(),
            new_id(),
        )
        async with db_container.db.user_session(scope) as sess:
            sess.add(
                RecordRow(
                    id=rec_id,
                    user_id=user_id,
                    person_id=chloe_uuid,
                    kind="note",
                    title_enc=db_container.cipher.encrypt_str(
                        dek,
                        "温热桂花燕麦拿铁手札",
                        user_id=user_id,
                        table="records",
                        column="title_enc",
                        row_id=rec_id,
                    ),
                    body_enc=db_container.cipher.encrypt_str(
                        dek,
                        "生理期前三天忌冰美式",
                        user_id=user_id,
                        table="records",
                        column="body_enc",
                        row_id=rec_id,
                    ),
                    snippet_enc=db_container.cipher.encrypt_str(
                        dek,
                        "半糖温热燕麦奶",
                        user_id=user_id,
                        table="records",
                        column="snippet_enc",
                        row_id=rec_id,
                    ),
                )
            )
            await sess.flush()
            sess.add(
                FactRow(
                    id=fact_id,
                    user_id=user_id,
                    person_id=chloe_uuid,
                    record_id=rec_id,
                    type="taboo",
                    status="confirmed",
                    content_enc=db_container.cipher.encrypt_str(
                        dek,
                        "生理期前三天忌冰美式",
                        user_id=user_id,
                        table="facts",
                        column="content_enc",
                        row_id=fact_id,
                    ),
                )
            )
            await sess.flush()
            sess.add(
                ReminderRow(
                    id=rem_id,
                    user_id=user_id,
                    person_id=chloe_uuid,
                    fact_id=fact_id,
                    title_enc=db_container.cipher.encrypt_str(
                        dek,
                        "重要纪念日",
                        user_id=user_id,
                        table="reminders",
                        column="title_enc",
                        row_id=rem_id,
                    ),
                )
            )
            sess.add(ChatThreadRow(id=thread_id, user_id=user_id, person_id=chloe_uuid))
            await sess.flush()
            sess.add(
                ChatMessageRow(
                    id=msg_id,
                    user_id=user_id,
                    thread_id=thread_id,
                    person_id=chloe_uuid,
                    role="user",
                    content_enc=db_container.cipher.encrypt_str(
                        dek,
                        "明天海边散步怎么开场呀？",
                        user_id=user_id,
                        table="chat_messages",
                        column="content_enc",
                        row_id=msg_id,
                    ),
                    tier_snapshot="gold",
                )
            )

        # 1. Demote Chloe gold -> crystal: exact V10 response contract & prepended to casualIds[0]
        demote = await client.patch(
            f"/v1/persons/{ids['chloe']}/tier",
            headers=_auth_headers(token, idem_key="tier-demote-chloe"),
            json={"tier": "crystal"},
        )
        assert demote.status_code == 200
        d_body = demote.json()
        assert set(d_body.keys()) == {
            "personId",
            "previousTier",
            "tier",
            "coreIds",
            "casualIds",
            "version",
        }
        assert d_body["personId"] == ids["chloe"]
        assert d_body["previousTier"] == "gold"
        assert d_body["tier"] == "crystal"
        assert d_body["coreIds"] == [ids["xubai"], ids["zhixia"]]
        assert d_body["casualIds"] == [ids["chloe"], ids["prof"], ids["kevin"]]
        assert d_body["version"] == 2

        # 2. Switch Xubai gold -> silver: keeps exact position inside coreIds
        core_swap = await client.patch(
            f"/v1/persons/{ids['xubai']}/tier",
            headers=_auth_headers(token, idem_key="tier-swap-xubai"),
            json={"tier": "silver"},
        )
        assert core_swap.status_code == 200
        assert core_swap.json()["coreIds"] == [ids["xubai"], ids["zhixia"]]

        # 3. Promote Chloe crystal -> gold: appended to END of coreIds
        promote = await client.patch(
            f"/v1/persons/{ids['chloe']}/tier",
            headers=_auth_headers(token, idem_key="tier-promote-chloe"),
            json={"tier": "gold"},
        )
        assert promote.status_code == 200
        assert promote.json()["coreIds"] == [ids["xubai"], ids["zhixia"], ids["chloe"]]
        assert promote.json()["casualIds"] == [ids["prof"], ids["kevin"]]

        # Verify ZERO deletion/mutation of historical records, facts, reminders, chat messages!
        async with db_container.db.user_session(scope) as sess:
            assert await sess.scalar(select(func.count()).select_from(RecordRow)) == 1
            assert await sess.scalar(select(func.count()).select_from(FactRow)) == 1
            assert await sess.scalar(select(func.count()).select_from(ReminderRow)) == 1
            assert await sess.scalar(select(func.count()).select_from(ChatMessageRow)) == 1

        # 4. Reorder Chloe back to front of coreIds via POST /persons/{id}:move
        moved = await client.post(
            f"/v1/persons/{ids['chloe']}:move",
            headers=_auth_headers(token, idem_key="move-chloe-front"),
            json={"beforePersonId": ids["xubai"]},
        )
        assert moved.status_code == 200
        assert moved.json()["coreIds"] == [ids["chloe"], ids["xubai"], ids["zhixia"]]

        # 5. Concurrent tier transitions lock via FOR UPDATE with zero lost members or duplicate ranks
        await asyncio.gather(
            client.patch(
                f"/v1/persons/{ids['prof']}/tier",
                headers=_auth_headers(token, idem_key="conc-prof-gold"),
                json={"tier": "gold"},
            ),
            client.patch(
                f"/v1/persons/{ids['kevin']}/tier",
                headers=_auth_headers(token, idem_key="conc-kevin-silver"),
                json={"tier": "silver"},
            ),
            client.patch(
                f"/v1/persons/{ids['zhixia']}/tier",
                headers=_auth_headers(token, idem_key="conc-zhixia-crystal"),
                json={"tier": "crystal"},
            ),
        )
        listed = (await client.get("/v1/persons", headers=_auth_headers(token))).json()
        assert set(listed["coreIds"]) == {
            ids["chloe"],
            ids["xubai"],
            ids["prof"],
            ids["kevin"],
        }
        assert listed["casualIds"] == [ids["zhixia"]]


@pytest.mark.asyncio
async def test_aliases_homonyms_and_entity_resolver_four_priorities(
    db_container: AppContainer,
) -> None:
    user_id, scope, token, dek = await _provision_user(db_container)
    app = _build_test_app(db_container)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        r_chloe = await client.post(
            "/v1/persons",
            headers=_auth_headers(token, idem_key="er-chloe"),
            json={"nameCn": "沈清秋", "tier": "gold"},
        )
        r_prof = await client.post(
            "/v1/persons",
            headers=_auth_headers(token, idem_key="er-prof"),
            json={"nameCn": "陈教授", "tier": "crystal", "aliases": ["老陈"]},
        )
        r_mgr = await client.post(
            "/v1/persons",
            headers=_auth_headers(token, idem_key="er-mgr"),
            json={"nameCn": "陈经理", "tier": "silver"},
        )
        r_sumin = await client.post(
            "/v1/persons",
            headers=_auth_headers(token, idem_key="er-sumin"),
            json={"nameCn": "苏敏姐", "tier": "crystal"},
        )

        chloe_id = UUID(r_chloe.json()["id"])
        prof_id = UUID(r_prof.json()["id"])
        mgr_id = UUID(r_mgr.json()["id"])
        sumin_id = UUID(r_sumin.json()["id"])

        # Add homonym nickname "老陈" to 陈经理 (allowed across different person_ids!)
        homonym_add = await client.post(
            f"/v1/persons/{mgr_id}/aliases",
            headers=_auth_headers(token, idem_key="alias-laochen-mgr"),
            json={"alias": "老陈", "kind": "nickname"},
        )
        assert homonym_add.status_code == 201
        mgr_laochen_alias_id = UUID(homonym_add.json()["id"])

        # Duplicate alias on SAME person -> 409 Conflict
        dup_same = await client.post(
            f"/v1/persons/{mgr_id}/aliases",
            headers=_auth_headers(token, idem_key="alias-laochen-mgr-dup"),
            json={"alias": " 老 陈 ", "kind": "nickname"},
        )
        assert dup_same.status_code == 409

        # Missing Idempotency-Key on POST /aliases -> 400
        no_idem_alias = await client.post(
            f"/v1/persons/{sumin_id}/aliases",
            headers=_auth_headers(token),
            json={"alias": "敏姐", "kind": "nickname"},
        )
        assert no_idem_alias.status_code == 400

        # Add unique alias "敏姐" to 苏敏姐
        sumin_alias = await client.post(
            f"/v1/persons/{sumin_id}/aliases",
            headers=_auth_headers(token, idem_key="alias-minjie"),
            json={"alias": "敏姐", "kind": "nickname"},
        )
        assert sumin_alias.status_code == 201

        # Verify PersonAliasRow exact AAD & blind-index domain="person_alias"
        sumin_alias_uuid = UUID(sumin_alias.json()["id"])
        async with db_container.db.user_session(scope) as sess:
            arow = await sess.get(PersonAliasRow, sumin_alias_uuid)
            assert arow is not None
            assert arow.alias_hmac == db_container.cipher.compute_blind_index(
                dek,
                user_id=user_id,
                domain="person_alias",
                normalized_value=normalize_alias_for_index("敏姐"),
            )
            assert (
                db_container.cipher.decrypt_str(
                    dek,
                    arow.alias_enc,
                    user_id=user_id,
                    table="person_aliases",
                    column="alias_enc",
                    row_id=sumin_alias_uuid,
                )
                == "敏姐"
            )

        # Primary alias deletion is blocked; secondary alias deletion works
        mgr_aliases = (
            await client.get(f"/v1/persons/{mgr_id}/aliases", headers=_auth_headers(token))
        ).json()["items"]
        primary_mgr_alias_id = next(a["id"] for a in mgr_aliases if a["isPrimary"])
        del_primary = await client.delete(
            f"/v1/persons/{mgr_id}/aliases/{primary_mgr_alias_id}",
            headers=_auth_headers(token, idem_key="del-primary-mgr"),
        )
        assert del_primary.status_code == 409

        # Exercise EntityResolver across all 4 priorities inside user_session
        resolver = EntityResolver(db_container)
        async with db_container.db.user_session(scope) as sess:
            known = await resolver.list_known_persons(sess, scope, dek)
            assert {kp.person_id for kp in known} == {chloe_id, prof_id, mgr_id, sumin_id}

            # Priority 1a: CURRENT mode hard-locks scoped_person_id on pronouns / unmarked clauses
            res_p1_lock = await resolver.resolve(
                sess,
                scope,
                dek,
                mention="她",
                clause_text="她生理期前三天忌冰美式",
                route_mode=RouteMode.CURRENT,
                scoped_person_id=chloe_id,
            )
            assert res_p1_lock.person_id == chloe_id
            assert res_p1_lock.scoped_locked is True
            assert res_p1_lock.ambiguous is False

            # Priority 1b: CURRENT mode splits out when clause explicitly names a DIFFERENT known person
            res_p1_other = await resolver.resolve(
                sess,
                scope,
                dek,
                mention="敏姐",
                clause_text="另外敏姐说周四下午审片",
                route_mode=RouteMode.CURRENT,
                scoped_person_id=chloe_id,
            )
            assert res_p1_other.person_id == sumin_id
            assert res_p1_other.scoped_locked is False

            # Priority 2a: Exact blind-index unique match
            res_p2_unique = await resolver.resolve(
                sess,
                scope,
                dek,
                mention="敏姐",
                route_mode=RouteMode.GLOBAL,
            )
            assert res_p2_unique.person_id == sumin_id
            assert res_p2_unique.matched_via == "blind_index"

            # Priority 2b: Homonym disambiguation on "老陈"
            res_p2_homonym = await resolver.resolve(
                sess,
                scope,
                dek,
                mention="老陈",
                route_mode=RouteMode.GLOBAL,
            )
            assert res_p2_homonym.person_id is None
            assert res_p2_homonym.ambiguous is True
            assert set(res_p2_homonym.candidate_person_ids) == {prof_id, mgr_id}

            # Priority 3: Substring match ("清秋" -> "沈清秋")
            res_p3_sub = await resolver.resolve(
                sess,
                scope,
                dek,
                mention="清秋",
                route_mode=RouteMode.GLOBAL,
            )
            assert res_p3_sub.person_id == chloe_id
            assert res_p3_sub.matched_via == "known_substring"

            # Priority 4: Unknown person in GLOBAL mode provisions a provisional crystal person
            res_p4_new = await resolver.resolve(
                sess,
                scope,
                dek,
                mention="王律师",
                route_mode=RouteMode.GLOBAL,
            )
            assert res_p4_new.person_id is not None
            assert res_p4_new.is_new_person is True
            assert res_p4_new.person_state is PersonState.PROVISIONAL
            assert res_p4_new.tier is Tier.CRYSTAL
            lawyer_id = res_p4_new.person_id

            # Second mention of "王律师" hits blind index on the same provisional person!
            res_p4_repeat = await resolver.resolve(
                sess,
                scope,
                dek,
                mention="王律师",
                route_mode=RouteMode.GLOBAL,
            )
            assert res_p4_repeat.person_id == lawyer_id
            assert res_p4_repeat.is_new_person is False
            assert res_p4_repeat.matched_via == "blind_index"

        # Delete secondary alias on 陈经理 -> "老陈" now uniquely resolves to 陈教授!
        del_sec = await client.delete(
            f"/v1/persons/{mgr_id}/aliases/{mgr_laochen_alias_id}",
            headers=_auth_headers(token, idem_key="del-sec-laochen"),
        )
        assert del_sec.status_code == 204

        async with db_container.db.user_session(scope) as sess:
            res_after_del = await resolver.resolve(
                sess, scope, dek, mention="老陈", route_mode=RouteMode.GLOBAL
            )
            assert res_after_del.person_id == prof_id
            assert res_after_del.ambiguous is False

        # Verify ?includeProvisional=false filters out provisional persons
        excl_prov = (
            await client.get("/v1/persons?includeProvisional=false", headers=_auth_headers(token))
        ).json()
        assert str(lawyer_id) not in excl_prov["casualIds"]
        assert all(it["id"] != str(lawyer_id) for it in excl_prov["items"])


@pytest.mark.asyncio
async def test_photos_exif_strip_bootstrap_top10_materials_isolation_and_soft_delete(
    db_container: AppContainer,
) -> None:
    user_a, scope_a, token_a, dek_a = await _provision_user(db_container)
    _, _, token_b, _ = await _provision_user(db_container)
    app = _build_test_app(db_container)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        r_person = await client.post(
            "/v1/persons",
            headers=_auth_headers(token_a, idem_key="photo-person"),
            json={
                "nameCn": "沈清秋",
                "roleTag": "心动伴侣",
                "tier": "gold",
                "traits": ["偏爱拍立得"],
            },
        )
        person_id = UUID(r_person.json()["id"])

        # Missing Idempotency-Key on photo upload must return 400
        missing_idem_up = await client.post(
            f"/v1/persons/{person_id}/photos/avatar",
            headers=_auth_headers(token_a),
            files={"file": ("broken.jpg", b"not-a-real-image", "image/jpeg")},
        )
        assert missing_idem_up.status_code == 400

        # Reject invalid non-image payload when Idempotency-Key is supplied
        bad_img = await client.post(
            f"/v1/persons/{person_id}/photos/avatar",
            headers=_auth_headers(token_a, idem_key="bad-img-1"),
            files={"file": ("broken.jpg", b"not-a-real-image", "image/jpeg")},
        )
        assert bad_img.status_code == 422

        # Upload JPEG containing EXIF metadata
        jpeg_with_exif = _make_jpeg_with_exif()
        uploaded = await client.post(
            f"/v1/persons/{person_id}/photos/avatar",
            headers=_auth_headers(token_a, idem_key="upload-avatar-1"),
            files={"file": ("avatar.jpg", jpeg_with_exif, "image/jpeg")},
        )
        assert uploaded.status_code == 200
        up_json = uploaded.json()
        assert up_json["hasPhoto"] is True
        signed_avatar_url = up_json["avatarUrl"]
        assert signed_avatar_url.startswith(
            f"/v1/persons/{person_id}/photos/avatar?uid={user_a}&exp="
        )
        assert "&sig=" in signed_avatar_url

        # Verify blob on disk is encrypted (no plaintext JPEG header or EXIF string on disk!)
        blob_files = [
            p
            for p in db_container.settings.blob_storage_dir.rglob("*")
            if p.is_file() and not p.name.endswith(".ct")
        ]
        assert len(blob_files) == 1
        disk_bytes = blob_files[0].read_bytes()
        assert not disk_bytes.startswith(b"\xff\xd8\xff")
        assert b"SecretAuthorName" not in disk_bytes

        # Stream decrypted photo back via Bearer JWT -> valid JPEG with ZERO EXIF metadata!
        dl = await client.get(
            f"/v1/persons/{person_id}/photos/avatar", headers=_auth_headers(token_a)
        )
        assert dl.status_code == 200
        assert dl.headers["content-type"] == "image/jpeg"
        with Image.open(BytesIO(dl.content)) as stripped_img:
            assert stripped_img.size == (64, 48)
            assert len(stripped_img.getexif()) == 0

        # Browser <img src> signed URL works WITHOUT Authorization header, while tampered sig fails!
        dl_signed = await client.get(signed_avatar_url)
        assert dl_signed.status_code == 200
        assert dl_signed.content == dl.content

        tampered_url = signed_avatar_url[:-4] + (
            "0000" if signed_avatar_url[-4:] != "0000" else "ffff"
        )
        dl_tampered = await client.get(tampered_url)
        assert dl_tampered.status_code == 401

        # Tenant B cannot access Tenant A's person or photo (RLS + scope isolation)
        assert (
            await client.get(f"/v1/persons/{person_id}", headers=_auth_headers(token_b))
        ).status_code == 404
        assert (
            await client.get(
                f"/v1/persons/{person_id}/photos/avatar", headers=_auth_headers(token_b)
            )
        ).status_code == 404

        # Seed 13 records (10 normal + 1 photo + 1 held + 1 soft-deleted), 1 active reminder, and 3 inbox items
        base_ts = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
        voice_media_key = f"{user_a}/records/voice_{new_id()}.webm"
        photo_media_key = f"{user_a}/records/photo_{new_id()}.jpg"
        await db_container.storage.put_encrypted(
            user_id=user_a,
            dek=dek_a,
            object_key=voice_media_key,
            plaintext=b"fake-webm-audio",
            content_type="audio/webm",
        )
        await db_container.storage.put_encrypted(
            user_id=user_a,
            dek=dek_a,
            object_key=photo_media_key,
            plaintext=dl.content,
            content_type="image/jpeg",
        )

        rem_id = new_id()
        async with db_container.db.user_session(scope_a) as sess:
            for idx in range(13):
                rid = new_id()
                is_deleted = idx == 12
                is_held = idx == 11
                is_voice = idx == 10
                is_photo = idx == 9
                kind = "voice" if is_voice else ("photo" if is_photo else "note")
                media_key = voice_media_key if is_voice else (photo_media_key if is_photo else None)
                sess.add(
                    RecordRow(
                        id=rid,
                        user_id=user_a,
                        person_id=person_id,
                        kind=kind,
                        title_enc=db_container.cipher.encrypt_str(
                            dek_a,
                            f"手札标题-{idx:02d}",
                            user_id=user_a,
                            table="records",
                            column="title_enc",
                            row_id=rid,
                        ),
                        body_enc=db_container.cipher.encrypt_str(
                            dek_a,
                            f"正文内容-{idx:02d}",
                            user_id=user_a,
                            table="records",
                            column="body_enc",
                            row_id=rid,
                        ),
                        snippet_enc=db_container.cipher.encrypt_str(
                            dek_a,
                            f"摘要-{idx:02d}",
                            user_id=user_a,
                            table="records",
                            column="snippet_enc",
                            row_id=rid,
                        ),
                        media_object_key=media_key,
                        audio_duration_ms=18_000 if is_voice else None,
                        stamp_code="KAMAKURA · 04.17" if is_voice else None,
                        happened_on=date(2026, 4, 17),
                        review_state="held" if is_held else "normal",
                        deleted_at=base_ts if is_deleted else None,
                        created_at=base_ts + timedelta(minutes=idx),
                        updated_at=base_ts + timedelta(minutes=idx),
                    )
                )

            sess.add(
                ReminderRow(
                    id=rem_id,
                    user_id=user_a,
                    person_id=person_id,
                    title_enc=db_container.cipher.encrypt_str(
                        dek_a,
                        "恋爱纪念日提醒",
                        user_id=user_a,
                        table="reminders",
                        column="title_enc",
                        row_id=rem_id,
                    ),
                    rule="once",
                    once_date=date(2026, 12, 25),
                    next_fire_at=base_ts + timedelta(days=30),
                    status="active",
                )
            )

            for idx, f_status in enumerate(("confirmed", "pending")):
                fid = new_id()
                sess.add(
                    FactRow(
                        id=fid,
                        user_id=user_a,
                        person_id=person_id,
                        type="preference",
                        polarity="positive",
                        content_enc=db_container.cipher.encrypt_str(
                            dek_a,
                            f"敏感偏好-{idx}",
                            user_id=user_a,
                            table="facts",
                            column="content_enc",
                            row_id=fid,
                        ),
                        status=f_status,
                        created_by="user",
                        valid_at=base_ts,
                        created_at=base_ts,
                        updated_at=base_ts,
                    )
                )

            # 2 open inbox items + 1 pre-resolved inbox item -> inboxCount == 2
            open_inbox_ids: set[UUID] = set()
            preresolved_inbox_id = new_id()
            for idx, st in enumerate(("open", "open", "resolved")):
                iid = preresolved_inbox_id if st == "resolved" else new_id()
                if st == "open":
                    open_inbox_ids.add(iid)
                sess.add(
                    InboxItemRow(
                        id=iid,
                        user_id=user_a,
                        person_id=person_id,
                        kind="fact_confirm",
                        status=st,
                        resolution_action="confirm" if st == "resolved" else None,
                        resolved_at=base_ts if st == "resolved" else None,
                        payload_enc=db_container.cipher.encrypt_json(
                            dek_a,
                            {"n": idx},
                            user_id=user_a,
                            table="inbox_items",
                            column="payload_enc",
                            row_id=iid,
                        ),
                    )
                )

        # Verify GET /v1/bootstrap returns V10 gleb-app.mjs fields, signed URLs, & excludes held/deleted records
        boot = await client.get("/v1/bootstrap", headers=_auth_headers(token_a))
        assert boot.status_code == 200
        bdata = boot.json()
        assert bdata["coreIds"] == [str(person_id)]
        assert bdata["casualIds"] == []
        assert bdata["inboxCount"] == 2
        p_entry = bdata["people"][str(person_id)]
        assert p_entry["id"] == str(person_id)
        assert p_entry["nameCn"] == "沈清秋"
        assert p_entry["cupidTitle"] == "沈清秋的丘比特"
        assert p_entry["hasPhoto"] is True
        assert p_entry["avatarUrl"].startswith(
            f"/v1/persons/{person_id}/photos/avatar?uid={user_a}&exp="
        )
        assert p_entry["avatar"] == p_entry["avatarUrl"]
        assert "mainPhoto" in p_entry and p_entry["mainPhoto"] is None
        assert "subPhoto" in p_entry and p_entry["subPhoto"] is None
        assert p_entry["sealBg"] == p_entry["visualTokens"]["sealBg"]
        assert p_entry["sealColor"] == p_entry["visualTokens"]["sealColor"]
        assert p_entry["tapeColor"] == p_entry["visualTokens"]["tapeColor"]
        assert p_entry["tabColor"] == p_entry["visualTokens"]["tabColor"]
        assert all(bool(p_entry[k]) for k in ("sealBg", "sealColor", "tapeColor", "tabColor"))

        mats = p_entry["materials"]
        assert len(mats) == 10
        # idx=12 was soft-deleted and idx=11 was review_state='held', so newest visible is idx=10!
        assert all(m["title"] not in ("手札标题-11", "手札标题-12") for m in mats)
        assert mats[0]["title"] == "手札标题-10"
        assert mats[0]["type"] == "voice"
        assert mats[0]["dur"] == "0:18"
        assert mats[0]["stampCode"] == "KAMAKURA · 04.17"
        assert mats[0]["happenedOn"] == "2026-04-17"
        assert mats[0]["mediaUrl"].startswith(
            f"/v1/materials/{mats[0]['id']}/media?uid={user_a}&exp="
        )
        assert "thumb" in mats[0] and mats[0]["thumb"] is None

        # idx=9 is kind='photo', so thumb must equal signed mediaUrl!
        assert mats[1]["title"] == "手札标题-09"
        assert mats[1]["type"] == "photo"
        assert mats[1]["mediaUrl"].startswith(
            f"/v1/materials/{mats[1]['id']}/media?uid={user_a}&exp="
        )
        assert mats[1]["thumb"] == mats[1]["mediaUrl"]
        assert mats[-1]["title"] == "手札标题-01"

        # DELETE /v1/persons/{id} requires Idempotency-Key -> 400 when omitted
        del_no_idem = await client.delete(
            f"/v1/persons/{person_id}",
            headers=_auth_headers(token_a),
        )
        assert del_no_idem.status_code == 400

        # DELETE /v1/persons/{id} performs 30-day soft-delete (defers physical blob purge to worker)
        deleted = await client.delete(
            f"/v1/persons/{person_id}",
            headers=_auth_headers(token_a, idem_key="del-person-a"),
        )
        assert deleted.status_code == 204
        assert (
            await client.get(f"/v1/persons/{person_id}", headers=_auth_headers(token_a))
        ).status_code == 404
        assert (await client.get(signed_avatar_url)).status_code == 404
        boot_after_del = (await client.get("/v1/bootstrap", headers=_auth_headers(token_a))).json()
        assert str(person_id) not in boot_after_del["people"]
        assert str(person_id) not in boot_after_del["coreIds"]

        async with db_container.db.user_session(scope_a) as sess:
            p_row = await sess.scalar(select(PersonRow).where(PersonRow.id == person_id))
            assert p_row is not None
            assert p_row.deleted_at is not None

            rec_rows = (
                await sess.scalars(select(RecordRow).where(RecordRow.person_id == person_id))
            ).all()
            assert len(rec_rows) == 13
            assert all(r.deleted_at is not None for r in rec_rows)

            fact_rows = (
                await sess.scalars(select(FactRow).where(FactRow.person_id == person_id))
            ).all()
            assert len(fact_rows) == 2
            assert all(f.status == "rejected" and f.invalid_at is not None for f in fact_rows)

            rem_row = await sess.scalar(select(ReminderRow).where(ReminderRow.id == rem_id))
            assert rem_row is not None
            assert rem_row.status == "cancelled"
            assert rem_row.next_fire_at is None

            inbox_rows = (
                await sess.scalars(select(InboxItemRow).where(InboxItemRow.person_id == person_id))
            ).all()
            assert all(it.status == "resolved" and it.resolved_at is not None for it in inbox_rows)
            by_iid = {it.id: it for it in inbox_rows}
            for oid in open_inbox_ids:
                assert by_iid[oid].resolution_action == "discard"
            assert by_iid[preresolved_inbox_id].resolution_action == "confirm"

        # Encrypted blobs remain on disk pending the 30-day purge_soft_deleted worker job
        remaining_blobs = [
            p
            for p in db_container.settings.blob_storage_dir.rglob("*")
            if p.is_file() and not p.name.endswith(".ct")
        ]
        assert len(remaining_blobs) == 3


@pytest.mark.asyncio
async def test_idempotency_replay_resigns_expired_media_urls_after_16m(
    db_container: AppContainer,
) -> None:
    user_id, _, token_t0, _ = await _provision_user(db_container)
    assert isinstance(db_container.clock, ManualClock)
    app = _build_test_app(db_container)
    jpeg_bytes = _make_jpeg_with_exif()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        created = await client.post(
            "/v1/persons",
            headers=_auth_headers(token_t0, idem_key="r207-create"),
            json={"nameCn": "宋词", "tier": "gold"},
        )
        assert created.status_code == 201
        person_id = UUID(created.json()["id"])

        up_avatar = await client.post(
            f"/v1/persons/{person_id}/photos/avatar",
            headers=_auth_headers(token_t0, idem_key="r207-up-avatar"),
            files={"file": ("avatar.jpg", jpeg_bytes, "image/jpeg")},
        )
        assert up_avatar.status_code == 200
        old_avatar_url = up_avatar.json()["avatarUrl"]

        up_main = await client.post(
            f"/v1/persons/{person_id}/photos/main",
            headers=_auth_headers(token_t0, idem_key="r207-up-main"),
            files={"file": ("main.jpg", jpeg_bytes, "image/jpeg")},
        )
        assert up_main.status_code == 200

        up_sub = await client.post(
            f"/v1/persons/{person_id}/photos/sub",
            headers=_auth_headers(token_t0, idem_key="r207-up-sub"),
            files={"file": ("sub.jpg", jpeg_bytes, "image/jpeg")},
        )
        assert up_sub.status_code == 200

        patched = await client.patch(
            f"/v1/persons/{person_id}",
            headers=_auth_headers(token_t0, idem_key="r207-patch", if_match=4),
            json={"shortMemo": "初见于江南"},
        )
        assert patched.status_code == 200

        # Advance 16 minutes (> 15m media signature TTL, still inside 24h idempotency TTL)
        db_container.clock.advance(seconds=960)
        token_t16, _ = db_container.tokens.create_access_token(
            user_id, now=db_container.clock.now()
        )

        # Previously issued signed URL is now expired -> 401
        assert (await client.get(old_avatar_url)).status_code == 401

        # Replaying photo upload & patch with original Idempotency-Keys freshly re-signs all non-null photo URLs!
        replay_avatar = await client.post(
            f"/v1/persons/{person_id}/photos/avatar",
            headers=_auth_headers(token_t16, idem_key="r207-up-avatar"),
            files={"file": ("avatar.jpg", jpeg_bytes, "image/jpeg")},
        )
        assert replay_avatar.status_code == 200
        fresh_avatar_url = replay_avatar.json()["avatarUrl"]
        assert fresh_avatar_url != old_avatar_url
        assert (await client.get(fresh_avatar_url)).status_code == 200

        replay_patch = await client.patch(
            f"/v1/persons/{person_id}",
            headers=_auth_headers(token_t16, idem_key="r207-patch", if_match=4),
            json={"shortMemo": "初见于江南"},
        )
        assert replay_patch.status_code == 200
        rp_json = replay_patch.json()
        for url_key in ("avatarUrl", "mainPhotoUrl", "subPhotoUrl"):
            assert rp_json[url_key] is not None
            assert (await client.get(rp_json[url_key])).status_code == 200
