"""Live PostgreSQL Row-Level Security (RLS) tenant-isolation tests (Invariant I1)."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from mindweave.core.dependencies import AppContainer
from mindweave.core.ids import new_id
from mindweave.core.orm import PersonRow, UserRow
from mindweave.shared.scopes import UserScope


@pytest.mark.asyncio
async def test_postgres_rls_strictly_isolates_tenants(db_container: AppContainer) -> None:
    user_a = new_id()
    user_b = new_id()
    person_a = new_id()
    person_b = new_id()

    async with db_container.db.system_session() as sys_sess:
        sys_sess.add_all([UserRow(id=user_a), UserRow(id=user_b)])

    scope_a = UserScope(user_a)
    scope_b = UserScope(user_b)

    async with db_container.db.user_session(scope_a) as sess_a:
        sess_a.add(
            PersonRow(
                id=person_a,
                user_id=user_a,
                tier="gold",
                rank="i",
                display_name_enc=b"enc-a",
                monogram="沈",
            )
        )

    async with db_container.db.user_session(scope_b) as sess_b:
        sess_b.add(
            PersonRow(
                id=person_b,
                user_id=user_b,
                tier="silver",
                rank="i",
                display_name_enc=b"enc-b",
                monogram="林",
            )
        )

    # Even if a developer forgets `WHERE user_id = ...`, PostgreSQL RLS ONLY returns user_a's row!
    async with db_container.db.user_session(scope_a) as sess_a:
        rows = (await sess_a.scalars(select(PersonRow))).all()
        assert [r.id for r in rows] == [person_a]

    # Unauthenticated session on app_engine (no app.user_id set) sees ZERO rows without crashing
    async with db_container.db.unauthenticated_app_session() as anon_sess:
        anon_rows = (await anon_sess.scalars(select(PersonRow))).all()
        assert anon_rows == []

    # Attempting to forge/insert a row for user_b while inside user_a's session is blocked by RLS WITH CHECK!
    with pytest.raises(DBAPIError):
        async with db_container.db.user_session(scope_a) as sess_a:
            sess_a.add(
                PersonRow(
                    id=new_id(),
                    user_id=user_b,
                    tier="crystal",
                    rank="m",
                    display_name_enc=b"forged",
                    monogram="陈",
                )
            )
