"""Versioned prompt registry with YAML frontmatter validation and SHA-256 content hashing (Invariant I9)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

import yaml

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n(.*)\Z", re.DOTALL)
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[A-Za-z0-9._-]+)?$")


class PromptRegistryError(ValueError):
    """Raised when prompt files are missing, malformed, or fail variable rendering."""


class RenderedPrompt(NamedTuple):
    """Immutable rendered prompt tuple `(rendered_text, prompt_id, version, sha256)`."""

    rendered_text: str
    prompt_id: str
    version: str
    sha256: str

    @property
    def version_tag(self) -> str:
        return f"{self.prompt_id}@{self.version}"


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    """Validated prompt definition loaded from a versioned markdown file."""

    id: str
    version: str
    task: str
    body: str
    sha256: str
    source_path: Path
    placeholders: frozenset[str]

    @property
    def version_tag(self) -> str:
        return f"{self.id}@{self.version}"


def _stringify_variable(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def parse_prompt_markdown(raw_text: str, *, source_path: Path) -> PromptTemplate:
    normalized = raw_text.replace("\r\n", "\n")
    match = _FRONTMATTER_RE.match(normalized)
    if match is None:
        raise PromptRegistryError(
            f"Prompt file {source_path.name} is missing valid YAML frontmatter delimited by '---'"
        )

    frontmatter_raw, body_raw = match.group(1), match.group(2)
    try:
        meta = yaml.safe_load(frontmatter_raw)
    except yaml.YAMLError as exc:
        raise PromptRegistryError(
            f"Prompt file {source_path.name} has invalid YAML frontmatter"
        ) from exc

    if not isinstance(meta, dict):
        raise PromptRegistryError(
            f"Prompt file {source_path.name} frontmatter must be a YAML mapping"
        )

    prompt_id = meta.get("id")
    version = meta.get("version")
    task = meta.get("task")

    if not isinstance(prompt_id, str) or not prompt_id.strip():
        raise PromptRegistryError(
            f"Prompt file {source_path.name} frontmatter requires non-empty string 'id'"
        )
    if not isinstance(version, str) or not _SEMVER_RE.match(version.strip()):
        raise PromptRegistryError(
            f"Prompt file {source_path.name} frontmatter requires semver 'version' (e.g. '1.1.0')"
        )
    if not isinstance(task, str) or not task.strip():
        raise PromptRegistryError(
            f"Prompt file {source_path.name} frontmatter requires non-empty string 'task'"
        )

    body = body_raw.strip()
    if not body:
        raise PromptRegistryError(f"Prompt file {source_path.name} has an empty prompt body")

    sha256_hex = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    placeholders = frozenset(_PLACEHOLDER_RE.findall(body))

    return PromptTemplate(
        id=prompt_id.strip(),
        version=version.strip(),
        task=task.strip(),
        body=body,
        sha256=sha256_hex,
        source_path=source_path,
        placeholders=placeholders,
    )


class PromptRegistry:
    """Loads, validates, hashes, and renders versioned `.md` system/task prompts."""

    def __init__(self, prompts_dir: Path | None = None) -> None:
        default_dir = Path(__file__).resolve().parents[4] / "prompts"
        self._prompts_dir = prompts_dir or default_dir
        self._by_id: dict[str, PromptTemplate] = {}
        self.reload()

    @property
    def prompts_dir(self) -> Path:
        return self._prompts_dir

    def reload(self) -> None:
        if not self._prompts_dir.exists() or not self._prompts_dir.is_dir():
            raise PromptRegistryError(f"Prompts directory does not exist: {self._prompts_dir}")

        loaded: dict[str, PromptTemplate] = {}
        for md_path in sorted(self._prompts_dir.glob("*.md")):
            raw = md_path.read_text(encoding="utf-8")
            tpl = parse_prompt_markdown(raw, source_path=md_path)
            if tpl.id in loaded:
                raise PromptRegistryError(
                    f"Duplicate prompt id '{tpl.id}' in {md_path.name} and "
                    f"{loaded[tpl.id].source_path.name}"
                )
            loaded[tpl.id] = tpl

        if not loaded:
            raise PromptRegistryError(f"No .md prompt files found in {self._prompts_dir}")

        self._by_id = loaded

    def list_ids(self) -> list[str]:
        return sorted(self._by_id.keys())

    def get(self, prompt_id: str) -> PromptTemplate:
        tpl = self._by_id.get(prompt_id)
        if tpl is None:
            raise PromptRegistryError(f"Unknown prompt id: '{prompt_id}'")
        return tpl

    def render(self, prompt_id: str, **variables: Any) -> RenderedPrompt:
        """Render `prompt_id` in a single pass and return `(rendered_text, prompt_id, version, sha256)`."""
        tpl = self.get(prompt_id)
        missing = sorted(tpl.placeholders - set(variables.keys()))
        if missing:
            raise PromptRegistryError(
                f"Missing required variables for prompt '{prompt_id}': {', '.join(missing)}"
            )

        def _sub(match: re.Match[str]) -> str:
            var_name = match.group(1)
            return _stringify_variable(variables[var_name])

        rendered = _PLACEHOLDER_RE.sub(_sub, tpl.body)
        return RenderedPrompt(
            rendered_text=rendered,
            prompt_id=tpl.id,
            version=tpl.version,
            sha256=tpl.sha256,
        )
