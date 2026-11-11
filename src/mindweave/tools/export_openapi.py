"""Export deterministic OpenAPI 3.1 schema snapshot to `openapi/openapi.json`."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from mindweave.core.config import Settings
from mindweave.main import create_app


def generate_openapi_dict() -> dict[str, object]:
    app = create_app(settings=Settings(environment="dev", ai_primary_provider="fake"))
    schema: dict[str, object] = app.openapi()
    return schema


def export_openapi_snapshot(output_path: Path | None = None) -> Path:
    repo_root = Path(__file__).resolve().parents[3]
    target = output_path or (repo_root / "openapi" / "openapi.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    rendered = (
        json.dumps(generate_openapi_dict(), indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    )
    target.write_text(rendered, encoding="utf-8")
    return target


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export MindWeave OpenAPI 3.1 JSON snapshot")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Destination path (defaults to openapi/openapi.json)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify existing snapshot matches current FastAPI OpenAPI schema without writing",
    )
    args = parser.parse_args(argv)
    repo_root = Path(__file__).resolve().parents[3]
    target = args.out or (repo_root / "openapi" / "openapi.json")
    expected = (
        json.dumps(generate_openapi_dict(), indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    )
    if args.check:
        if not target.exists():
            raise SystemExit(f"OpenAPI snapshot missing at {target}")
        actual = target.read_text(encoding="utf-8")
        if actual != expected:
            raise SystemExit(
                f"OpenAPI snapshot drift detected at {target}. Run `make openapi` to update."
            )
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
