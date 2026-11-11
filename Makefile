.PHONY: help sync dev worker lint format typecheck arch unit integration test migrate openapi eval

UV ?= uv

help:
	@echo "MindWeave Backend Make Targets:"
	@echo "  make lint       - Run Ruff linter & formatting checks"
	@echo "  make typecheck  - Run strict MyPy type checker across src/"
	@echo "  make arch       - Verify hexagonal import contracts & architecture invariants"
	@echo "  make test       - Run full unit, architecture, contract, and integration test suite"
	@echo "  make migrate    - Upgrade PostgreSQL schema to Alembic head"
	@echo "  make openapi    - Export OpenAPI 3.1 schema snapshot to openapi/openapi.json"
	@echo "  make eval       - Run offline deterministic AI evaluation suite"

sync:
	$(UV) sync --extra dev

dev:
	$(UV) run uvicorn mindweave.main:app --reload --host 127.0.0.1 --port 8000

worker:
	$(UV) run python -m mindweave.workers

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .

format:
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

typecheck:
	$(UV) run mypy src

arch:
	$(UV) run lint-imports
	$(UV) run pytest tests/architecture -q

unit:
	$(UV) run pytest tests/unit -q

integration:
	$(UV) run pytest tests/integration -q

test:
	$(UV) run pytest -q

migrate:
	$(UV) run alembic upgrade head

openapi:
	$(UV) run python -c "import importlib.util, json, pathlib, runpy; pathlib.Path('openapi').mkdir(parents=True, exist_ok=True); (runpy.run_module('mindweave.tools.export_openapi', run_name='__main__') if importlib.util.find_spec('mindweave.tools.export_openapi') else pathlib.Path('openapi/openapi.json').write_text(json.dumps((getattr(__import__('mindweave.main', fromlist=['app']), 'create_app', None) or (lambda: __import__('mindweave.main', fromlist=['app']).app))().openapi(), indent=2, ensure_ascii=False) + '\n', encoding='utf-8'))"

eval:
	$(UV) run python -m evals --mode offline
