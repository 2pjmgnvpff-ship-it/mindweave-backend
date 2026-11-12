"""Structured JSON extraction, validation, and 1-shot schema repair prompt builder."""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, ValidationError

_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


class StructuredOutputValidationError(ValueError):
    """Raised when provider JSON output cannot be parsed or fails Pydantic validation."""


def extract_json_object(raw_text: str) -> str:
    """Extract a JSON object payload from raw LLM text (stripping code fences & trailing commas)."""
    text = (raw_text or "").strip()
    if not text:
        raise StructuredOutputValidationError("Empty structured output from provider")

    fence_match = _CODE_FENCE_RE.search(text)
    if fence_match is not None:
        text = fence_match.group(1).strip()

    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]

    text = _TRAILING_COMMA_RE.sub(r"\1", text)
    return text


def parse_and_validate_json[ModelT: BaseModel](raw_text: str, schema_cls: type[ModelT]) -> ModelT:
    """Parse raw JSON string and validate against `schema_cls`."""
    extracted = extract_json_object(raw_text)
    try:
        parsed = json.loads(extracted)
    except json.JSONDecodeError as exc:
        raise StructuredOutputValidationError(f"Invalid JSON syntax: {exc.msg}") from exc

    try:
        return schema_cls.model_validate(parsed)
    except ValidationError as exc:
        sanitized = [
            {
                "loc": [str(p) for p in err.get("loc", ())],
                "msg": str(err.get("msg", "invalid")),
                "type": str(err.get("type", "value_error")),
            }
            for err in exc.errors()
        ]
        raise StructuredOutputValidationError(
            f"Schema validation failed for {schema_cls.__name__}: {json.dumps(sanitized, ensure_ascii=False)}"
        ) from exc


def build_json_repair_prompt(
    *,
    broken_output: str,
    schema_cls: type[BaseModel],
    validation_error: Exception,
) -> str:
    """Build a 1-shot JSON repair prompt instructing the model to emit strictly valid JSON."""
    schema_json = json.dumps(schema_cls.model_json_schema(), ensure_ascii=False)
    return (
        "你上一次返回的 JSON 无法通过 Schema 校验。请仅输出修复后的合法 JSON 对象，"
        "严禁包含 Markdown 代码块或任何解释文字。\n"
        f"<validation_error>{validation_error}</validation_error>\n"
        f"<target_json_schema>{schema_json}</target_json_schema>\n"
        f"<previous_invalid_output>{broken_output}</previous_invalid_output>"
    )
