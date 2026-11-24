"""Zero-PII AI call metering and cost estimation (Invariant I10, A-08 cost formula).

Records ONLY metadata (task, provider, model, prompt_id, prompt_version, prompt_hash,
token counts, latency_ms, estimated cost_micros_cny, outcome) into `ai_calls` — NEVER
user text, transcripts, replies, or person names.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.core.ids import new_id
from mindweave.core.logging import get_logger
from mindweave.core.orm import AiCallRow
from mindweave.shared.scopes import UserScope

logger = get_logger(__name__)

# Reference unit prices in CNY per 1,000,000 tokens (1 CNY = 1,000,000 micros_cny, so
# price_cny_per_1m_tokens == cost_micros_cny per single token!).
# Marked per A-08 cost transparency rule; offline `fake` adapter costs 0.
_MODEL_PRICE_CNY_PER_1M: dict[str, tuple[float, float]] = {
    "fake": (0.0, 0.0),
    "gemini-3.8-flash": (5.44, 27.19),
    "gemini-3.6-flash": (5.44, 27.19),
    "gemini-3.5-flash-lite": (2.18, 18.13),
    "gemini-2.5-flash-lite": (0.73, 2.90),
    "gemini-2.5-flash": (2.20, 18.00),
    "gemini-2.5-pro": (9.00, 72.00),
    "qwen-plus": (0.80, 2.00),
    "qwen-vl-max": (3.00, 9.00),
    "qwen3-omni-flash": (0.50, 1.50),
    "qwen3.8-omni-flash": (0.70, 2.10),
    "qwen3.5-omni-plus": (1.20, 3.60),
    "qwen-audio-turbo": (0.40, 1.20),
    "qwen-omni-turbo": (0.40, 1.20),
    "qwen2.5-omni-7b": (0.30, 0.90),
    "doubao-1-5-pro-32k": (0.80, 2.00),
    "deepseek-chat": (2.00, 8.00),
}


@dataclass(frozen=True, slots=True)
class MeteredCallRecord:
    """In-memory zero-PII mirror of an `AiCallRow` entry."""

    id: UUID
    user_id: UUID
    task: str
    provider: str
    model: str
    prompt_id: str | None
    prompt_version: str | None
    prompt_hash: str | None
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cost_micros_cny: int
    outcome: str


def estimate_cost_micros_cny(
    *,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> int:
    """Estimate call cost in micro-CNY (`1 CNY = 1_000_000 micros`) from token counts."""
    if provider == "fake":
        return 0
    in_tok = max(0, input_tokens)
    out_tok = max(0, output_tokens)
    in_rate, out_rate = _MODEL_PRICE_CNY_PER_1M.get(model, (1.50, 6.00))
    # Since 1 CNY = 1e6 micros and rates are CNY per 1e6 tokens, micros = tokens * rate!
    micros = round(in_tok * in_rate + out_tok * out_rate)
    return max(0, int(micros))


def estimate_token_count(text: str) -> int:
    """Deterministic token count fallback (~1 token per 2 chars for mixed CJK/ASCII)."""
    if not text:
        return 0
    return max(1, (len(text) + 1) // 2)


class AiCallMeter:
    """Persists zero-PII `AiCallRow` rows and retains a bounded ring buffer for inspection."""

    def __init__(self, *, max_recent: int = 512, db: Any | None = None) -> None:
        self._recent: deque[MeteredCallRecord] = deque(maxlen=max_recent)
        self._db: Any | None = db

    def bind_db(self, db: Any) -> None:
        """Bind a DatabaseManager for short post-call metering writes when `session=None`."""
        if self._db is None and db is not None:
            self._db = db

    @property
    def recent_records(self) -> list[MeteredCallRecord]:
        return list(self._recent)

    def clear_recent(self) -> None:
        self._recent.clear()

    @staticmethod
    def _build_row(entry: MeteredCallRecord) -> AiCallRow:
        return AiCallRow(
            id=entry.id,
            user_id=entry.user_id,
            task=entry.task,
            provider=entry.provider,
            model=entry.model,
            prompt_id=entry.prompt_id,
            prompt_version=entry.prompt_version,
            prompt_hash=entry.prompt_hash,
            input_tokens=entry.input_tokens,
            output_tokens=entry.output_tokens,
            latency_ms=entry.latency_ms,
            cost_micros_cny=entry.cost_micros_cny,
            outcome=entry.outcome,
        )

    async def record(
        self,
        *,
        scope: UserScope,
        task: str,
        provider: str,
        model: str,
        prompt_id: str | None,
        prompt_version: str | None,
        prompt_hash: str | None,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
        outcome: str,
        session: AsyncSession | None = None,
    ) -> MeteredCallRecord:
        in_tok = max(0, int(input_tokens))
        out_tok = max(0, int(output_tokens))
        lat_ms = max(0, int(latency_ms))
        cost_micros = estimate_cost_micros_cny(
            provider=provider,
            model=model,
            input_tokens=in_tok,
            output_tokens=out_tok,
        )
        call_id = new_id()
        entry = MeteredCallRecord(
            id=call_id,
            user_id=scope.user_id,
            task=task[:36],
            provider=provider[:24],
            model=model[:64],
            prompt_id=prompt_id[:64] if prompt_id else None,
            prompt_version=prompt_version[:32] if prompt_version else None,
            prompt_hash=prompt_hash[:64] if prompt_hash else None,
            input_tokens=in_tok,
            output_tokens=out_tok,
            latency_ms=lat_ms,
            cost_micros_cny=cost_micros,
            outcome=outcome[:24],
        )
        self._recent.append(entry)

        if session is not None:
            row = self._build_row(entry)
            try:
                async with session.begin_nested():
                    session.add(row)
                    await session.flush()
            except Exception as exc:
                logger.warning(
                    "ai_call_metering_persist_failed",
                    user_id=str(scope.user_id),
                    task=entry.task,
                    provider=entry.provider,
                    error_type=type(exc).__name__,
                )
        elif self._db is not None:
            row = self._build_row(entry)
            try:
                async with self._db.user_session(scope) as meter_sess:
                    meter_sess.add(row)
                    await meter_sess.flush()
            except Exception as exc:
                logger.warning(
                    "ai_call_metering_persist_failed",
                    user_id=str(scope.user_id),
                    task=entry.task,
                    provider=entry.provider,
                    error_type=type(exc).__name__,
                )

        return entry
