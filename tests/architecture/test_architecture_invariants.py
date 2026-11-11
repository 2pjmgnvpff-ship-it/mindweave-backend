"""Architecture Invariant Guardrail Tests (Invariants I1, I8, I9).

1. Every SQLAlchemy table in `Base.metadata.tables` MUST be in `SYSTEM_SCOPED_TABLES`
   or registered in `data_rights_registry` (Invariant I8).
2. All 19 `RLS_PROTECTED_TABLES` on live PostgreSQL MUST have both `ENABLE ROW LEVEL SECURITY`
   and `FORCE ROW LEVEL SECURITY` plus an active `tenant_isolation` policy, and `mindweave_app`
   MUST have `rolbypassrls == False` and `rolsuper == False` (Invariant I1).
3. `lint-imports` MUST exit with code 0 enforcing hexagonal domain purity & vendor SDK isolation.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

import pytest
from sqlalchemy import text

from mindweave.core import orm as _orm  # noqa: F401 — ensure all ORM models are registered
from mindweave.core.db import Base
from mindweave.core.dependencies import AppContainer
from mindweave.core.orm import RLS_PROTECTED_TABLES, SYSTEM_SCOPED_TABLES
from mindweave.shared.data_rights_registry import DataRightsRegistry, data_rights_registry

VALID_DELETION_STRATEGIES = frozenset({"cascade_from_users", "explicit_delete", "crypto_shred"})
FORBIDDEN_DOMAIN_IMPORT_PREFIXES: tuple[str, ...] = (
    "fastapi",
    "sqlalchemy",
    "httpx",
    "asyncpg",
    "google",
    "mindweave.core.orm",
    "mindweave.core.db",
    "mindweave.core.dependencies",
)


def assert_data_rights_completeness(
    table_names: Iterable[str],
    *,
    system_tables: Iterable[str],
    registry: DataRightsRegistry,
) -> None:
    sys_set = set(system_tables)
    registered = registry.all_registrations()
    unaccounted = sorted(t for t in table_names if t not in sys_set and t not in registered)
    if unaccounted:
        raise AssertionError(
            f"Invariant I8 violated — unregistered user-scoped table(s) in SQLAlchemy metadata: "
            f"{unaccounted}. Register every new user table in `data_rights_registry`!"
        )
    for name, reg in registered.items():
        if reg.deletion_strategy not in VALID_DELETION_STRATEGIES:
            raise AssertionError(
                f"Invalid deletion_strategy {reg.deletion_strategy!r} on table {name!r}"
            )


def test_invariant_i8_every_sqlalchemy_table_is_registered_for_data_rights() -> None:
    all_orm_tables = set(Base.metadata.tables.keys())
    assert len(RLS_PROTECTED_TABLES) == 19
    assert set(RLS_PROTECTED_TABLES).issubset(all_orm_tables)

    # Every ORM table must be either system-scoped or registered in DataRightsRegistry
    assert_data_rights_completeness(
        all_orm_tables,
        system_tables=SYSTEM_SCOPED_TABLES,
        registry=data_rights_registry,
    )

    # Specifically, ALL 19 RLS-protected tables must be explicitly registered in DataRightsRegistry
    registrations = data_rights_registry.all_registrations()
    for rls_table in RLS_PROTECTED_TABLES:
        assert rls_table in registrations, (
            f"RLS table {rls_table!r} missing from DataRightsRegistry"
        )

    # Negative check: adding an unregistered user table immediately fails the invariant gate
    with pytest.raises(AssertionError, match="unregistered_shadow_notes"):
        assert_data_rights_completeness(
            [*all_orm_tables, "unregistered_shadow_notes"],
            system_tables=SYSTEM_SCOPED_TABLES,
            registry=data_rights_registry,
        )


@pytest.mark.asyncio
async def test_invariant_i1_postgres_rls_enabled_forced_and_least_privilege_app_role(
    db_container: AppContainer,
) -> None:
    assert len(RLS_PROTECTED_TABLES) == 19

    async with db_container.db.system_session() as sys_sess:
        # 1. Verify relrowsecurity & relforcerowsecurity on pg_class for ALL 19 user-scoped tables
        class_rows = (
            await sys_sess.execute(
                text(
                    """
                    SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public' AND c.relkind = 'r'
                    """
                )
            )
        ).all()
        rls_flags_by_table = {
            str(relname): (bool(rls_enabled), bool(rls_forced))
            for relname, rls_enabled, rls_forced in class_rows
        }

        for tbl_name in RLS_PROTECTED_TABLES:
            assert tbl_name in rls_flags_by_table, (
                f"Table {tbl_name!r} missing in live PostgreSQL schema"
            )
            rls_enabled, rls_forced = rls_flags_by_table[tbl_name]
            assert rls_enabled is True, f"ENABLE ROW LEVEL SECURITY missing on {tbl_name!r}"
            assert rls_forced is True, f"FORCE ROW LEVEL SECURITY missing on {tbl_name!r}"

        # 2. Verify active `tenant_isolation` policy with USING & WITH CHECK bound to `app.user_id`
        policy_rows = (
            await sys_sess.execute(
                text(
                    """
                    SELECT tablename, policyname, qual, with_check
                    FROM pg_policies
                    WHERE schemaname = 'public' AND policyname = 'tenant_isolation'
                    """
                )
            )
        ).all()
        policies_by_table = {
            str(tablename): (str(qual or ""), str(with_check or ""))
            for tablename, _policyname, qual, with_check in policy_rows
        }

        for tbl_name in RLS_PROTECTED_TABLES:
            assert tbl_name in policies_by_table, (
                f"Active `tenant_isolation` RLS policy missing on {tbl_name!r}"
            )
            qual_expr, check_expr = policies_by_table[tbl_name]
            assert "app.user_id" in qual_expr, (
                f"Policy USING on {tbl_name!r} does not check app.user_id"
            )
            assert "app.user_id" in check_expr, (
                f"Policy WITH CHECK on {tbl_name!r} does not check app.user_id"
            )

        # 3. Verify `mindweave_app` database role is strictly non-superuser and NOBYPASSRLS
        role_row = (
            await sys_sess.execute(
                text(
                    """
                    SELECT rolsuper, rolbypassrls
                    FROM pg_roles
                    WHERE rolname = 'mindweave_app'
                    """
                )
            )
        ).one_or_none()
        assert role_row is not None, "Database role `mindweave_app` does not exist"
        rolsuper, rolbypassrls = bool(role_row[0]), bool(role_row[1])
        assert rolsuper is False, "`mindweave_app` role MUST NOT be a PostgreSQL superuser!"
        assert rolbypassrls is False, "`mindweave_app` role MUST NOT have BYPASSRLS!"


def test_hexagonal_layering_and_import_linter_contracts() -> None:
    repo_root = Path(__file__).resolve().parents[2]

    # Fast AST pre-check on all `modules/*/domain/**/*.py` files
    modules_root = repo_root / "src" / "mindweave" / "modules"
    for py_file in sorted(modules_root.glob("*/domain/**/*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            imported: list[str] = []
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
            for mod_name in imported:
                for forbidden in FORBIDDEN_DOMAIN_IMPORT_PREFIXES:
                    assert not (mod_name == forbidden or mod_name.startswith(f"{forbidden}.")), (
                        f"Domain purity violation in {py_file}: imports {mod_name!r}"
                    )

    # Full `lint-imports` contract verification across the entire codebase
    venv_lint_imports = Path(sys.executable).with_name("lint-imports")
    cmd = (
        [str(venv_lint_imports)]
        if venv_lint_imports.exists()
        else [shutil.which("lint-imports") or "lint-imports"]
    )
    proc = subprocess.run(
        cmd,
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"lint-imports failed with exit code {proc.returncode}:\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
