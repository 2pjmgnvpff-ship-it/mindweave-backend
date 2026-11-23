"""AI Model Gateway implementing `ModelGatewayPort` with PII masking, retries, circuit breaker, fallback, JSON repair, and zero-PII metering."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar
from uuid import UUID

import yaml
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from mindweave.ai.adapters.base import (
    AdapterCallRequest,
    AdapterCallResponse,
    AdapterError,
    AdapterTransientError,
    ProviderAdapter,
)
from mindweave.ai.adapters.fake import FakeAdapter
from mindweave.ai.adapters.gemini import GeminiAdapter
from mindweave.ai.adapters.openai_compatible import OpenAICompatibleAdapter
from mindweave.ai.metering import AiCallMeter, estimate_token_count
from mindweave.ai.ports import ModelGatewayPort
from mindweave.ai.prompts.registry import PromptRegistry, RenderedPrompt
from mindweave.ai.schemas.contracts import (
    CompanionChatInput,
    CompanionChatOutput,
    ConversationTurn,
    ExtractRecordInput,
    ExtractRecordOutput,
    SafetyClassifyOutput,
    ScreenshotParseOutput,
    TranscribeInput,
    TranscribeOutput,
)
from mindweave.ai.validators.asr_postprocess import postprocess_asr_transcript
from mindweave.ai.validators.citations import (
    StreamingCitationProcessor,
    enforce_companion_citations,
)
from mindweave.ai.validators.json_repair import (
    StructuredOutputValidationError,
    build_json_repair_prompt,
    parse_and_validate_json,
)
from mindweave.ai.validators.output_guard import enforce_companion_output_guard
from mindweave.core.clock import Clock, SystemClock
from mindweave.core.config import Settings
from mindweave.core.errors import UpstreamAIError
from mindweave.core.logging import get_logger
from mindweave.shared.pii import mask_hard_pii
from mindweave.shared.scopes import UserScope

logger = get_logger(__name__)

SchemaT = TypeVar("SchemaT", bound=BaseModel)


@dataclass
class CircuitBreaker:
    """Per-provider circuit breaker preventing cascading stalls on degraded upstream APIs."""

    provider_name: str
    failure_threshold: int = 3
    recovery_timeout_seconds: float = 30.0
    clock: Clock = field(default_factory=SystemClock)
    _consecutive_failures: int = 0
    _opened_at_epoch: float | None = None

    @property
    def state(self) -> str:
        if self._opened_at_epoch is None:
            return "closed"
        elapsed = self.clock.now().timestamp() - self._opened_at_epoch
        if elapsed >= self.recovery_timeout_seconds:
            return "half_open"
        return "open"

    def allow_request(self) -> bool:
        return self.state in ("closed", "half_open")

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at_epoch = None

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._opened_at_epoch = self.clock.now().timestamp()


@dataclass(frozen=True, slots=True)
class TaskRouteSpec:
    prompt_id: str
    temperature: float
    timeout_seconds: float
    models: dict[str, str]


_PROD_CN_FORBIDDEN_PROVIDERS: frozenset[str] = frozenset({"gemini", "fake"})


@dataclass(frozen=True, slots=True)
class RoutingConfig:
    semantic_cache_enabled: bool
    cb_failure_threshold: int
    cb_recovery_timeout_seconds: float
    retry_max_attempts: int
    retry_initial_wait_seconds: float
    retry_max_wait_seconds: float
    retry_jitter_seconds: float
    tasks: dict[str, TaskRouteSpec]
    fallback_providers: tuple[str, ...] = ()


def load_routing_config(routing_path: Path | None = None) -> RoutingConfig:
    path = routing_path or (Path(__file__).resolve().parent / "routing.yaml")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid routing config in {path}")
    if bool(raw.get("semantic_cache_enabled", False)):
        raise ValueError(
            "Invariant I9 / Not-To-Do #6 violation: semantic response caching is strictly prohibited"
        )

    cb = raw.get("circuit_breaker") or {}
    rt = raw.get("retry") or {}
    fb_raw = raw.get("fallback_providers") or ()
    fallback_providers = tuple(
        str(p).strip() for p in fb_raw if str(p).strip() and str(p).strip() != "none"
    )
    tasks_raw = raw.get("tasks") or {}
    tasks: dict[str, TaskRouteSpec] = {}
    for task_name, t_cfg in tasks_raw.items():
        tasks[str(task_name)] = TaskRouteSpec(
            prompt_id=str(t_cfg["prompt_id"]),
            temperature=float(t_cfg.get("temperature", 0.2)),
            timeout_seconds=float(t_cfg.get("timeout_seconds", 18.0)),
            models={str(k): str(v) for k, v in (t_cfg.get("models") or {}).items()},
        )

    return RoutingConfig(
        semantic_cache_enabled=False,
        cb_failure_threshold=int(cb.get("failure_threshold", 3)),
        cb_recovery_timeout_seconds=float(cb.get("recovery_timeout_seconds", 30.0)),
        retry_max_attempts=int(rt.get("max_attempts", 3)),
        retry_initial_wait_seconds=float(rt.get("initial_wait_seconds", 0.05)),
        retry_max_wait_seconds=float(rt.get("max_wait_seconds", 0.5)),
        retry_jitter_seconds=float(rt.get("jitter_seconds", 0.05)),
        tasks=tasks,
        fallback_providers=fallback_providers,
    )


def _mask_extract_payload(payload: ExtractRecordInput) -> ExtractRecordInput:
    masked_persons = [
        p.model_copy(
            update={
                "role_tag": mask_hard_pii(p.role_tag).masked_text,
                "recent_facts": [mask_hard_pii(rf).masked_text for rf in p.recent_facts[:6]],
            }
        )
        for p in payload.known_persons
    ]
    return payload.model_copy(
        update={
            "text": mask_hard_pii(payload.text).masked_text,
            "known_persons": masked_persons,
            "recent_history_hints": [
                mask_hard_pii(h).masked_text for h in payload.recent_history_hints
            ],
        }
    )


def _sanitize_extract_output(
    out: ExtractRecordOutput,
    payload: ExtractRecordInput,
) -> ExtractRecordOutput:
    """Strip any hallucinated person_id not present in `payload.known_persons`."""
    valid_ids = {p.person_id for p in payload.known_persons}
    hallucinated_any = False

    clean_persons = []
    for p in out.persons:
        matched = p.matched_person_id
        if matched is not None and matched not in valid_ids:
            matched = None
            hallucinated_any = True
        cands = [cid for cid in p.candidate_person_ids if cid in valid_ids]
        if len(cands) != len(p.candidate_person_ids):
            hallucinated_any = True
        clean_persons.append(
            p.model_copy(
                update={
                    "matched_person_id": matched,
                    "candidate_person_ids": cands,
                }
            )
        )

    clean_facts = []
    for f in out.facts:
        pid = f.person_id
        if pid is not None and pid not in valid_ids:
            pid = None
            hallucinated_any = True
        clean_facts.append(f.model_copy(update={"person_id": pid}))

    if not hallucinated_any:
        return out

    return out.model_copy(
        update={
            "persons": clean_persons,
            "facts": clean_facts,
            "needs_disambiguation": (
                True if not out.scoped_person_locked else out.needs_disambiguation
            ),
        }
    )


def _mask_companion_payload(payload: CompanionChatInput) -> CompanionChatInput:
    return payload.model_copy(
        update={
            "role_tag": mask_hard_pii(payload.role_tag).masked_text,
            "traits": [mask_hard_pii(t).masked_text for t in payload.traits],
            "confirmed_facts": [
                f.model_copy(update={"content": mask_hard_pii(f.content).masked_text})
                for f in payload.confirmed_facts
            ],
            "referenced_materials": [
                m.model_copy(
                    update={
                        "title": mask_hard_pii(m.title).masked_text,
                        "body": mask_hard_pii(m.body).masked_text,
                    }
                )
                for m in payload.referenced_materials
            ],
            "rolling_summary": (
                mask_hard_pii(payload.rolling_summary).masked_text
                if payload.rolling_summary
                else None
            ),
            "recent_turns": [
                ConversationTurn(
                    role=turn.role,
                    content=mask_hard_pii(turn.content).masked_text,
                )
                for turn in payload.recent_turns
            ],
            "user_message": mask_hard_pii(payload.user_message).masked_text,
        }
    )


class ModelGateway(ModelGatewayPort):
    """Vendor-neutral AI Model Gateway with PII shield, resilience, and zero-PII metering."""

    def __init__(
        self,
        settings: Settings,
        *,
        prompts: PromptRegistry | None = None,
        meter: AiCallMeter | None = None,
        clock: Clock | None = None,
        adapters: dict[str, ProviderAdapter] | None = None,
        routing_path: Path | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock or SystemClock()
        self._prompts = prompts or PromptRegistry(settings.prompts_dir)
        self._meter = meter or AiCallMeter()
        self._routing = load_routing_config(routing_path)

        default_adapters: dict[str, ProviderAdapter] = {
            "fake": FakeAdapter(),
            "gemini": GeminiAdapter(api_key=settings.gemini_api_key),
            "dashscope": OpenAICompatibleAdapter(
                provider_name="dashscope",
                api_key=settings.dashscope_api_key,
                base_url=settings.dashscope_base_url,
                default_model=settings.dashscope_extract_model,
            ),
            "ark": OpenAICompatibleAdapter(
                provider_name="ark",
                api_key=settings.ark_api_key,
                base_url=settings.ark_base_url,
                default_model=settings.ark_chat_model,
            ),
            "deepseek": OpenAICompatibleAdapter(
                provider_name="deepseek",
                api_key=settings.deepseek_api_key,
                base_url=settings.deepseek_base_url,
                default_model=settings.deepseek_chat_model,
            ),
        }
        if adapters:
            default_adapters.update(adapters)
        self._adapters = default_adapters

        self._breakers: dict[str, CircuitBreaker] = {
            name: CircuitBreaker(
                provider_name=name,
                failure_threshold=self._routing.cb_failure_threshold,
                recovery_timeout_seconds=self._routing.cb_recovery_timeout_seconds,
                clock=self._clock,
            )
            for name in self._adapters
        }

    @property
    def prompts(self) -> PromptRegistry:
        return self._prompts

    @property
    def meter(self) -> AiCallMeter:
        return self._meter

    @property
    def semantic_cache_enabled(self) -> bool:
        return False

    def get_circuit_breaker(self, provider: str) -> CircuitBreaker:
        return self._breakers[provider]

    def _assert_provider_allowed(self, provider_name: str) -> None:
        normalized = provider_name.strip().lower()
        if self._settings.environment == "prod_cn" and normalized in _PROD_CN_FORBIDDEN_PROVIDERS:
            raise UpstreamAIError(
                f"Compliance Gate M6: provider '{provider_name}' is prohibited in prod_cn",
                extensions={"provider": provider_name, "environment": "prod_cn"},
            )

    def _provider_chain(self) -> list[str]:
        chain: list[str] = [self._settings.ai_primary_provider]
        fb = self._settings.ai_fallback_provider
        if fb != "none" and fb not in chain:
            chain.append(fb)
        for route_fb in self._routing.fallback_providers:
            if route_fb not in chain:
                chain.append(route_fb)
        for provider_name in chain:
            self._assert_provider_allowed(provider_name)
        return chain

    def _provider_chain_for_task(self, task: str) -> list[str]:
        base_chain = self._provider_chain()
        if task == "transcribe" and self._settings.ai_asr_provider != "inherit":
            asr_provider = self._settings.ai_asr_provider
            self._assert_provider_allowed(asr_provider)
            chain: list[str] = [asr_provider]
            for provider_name in base_chain:
                if provider_name not in chain:
                    chain.append(provider_name)
            return chain
        return base_chain

    def _resolve_model(self, task: str, provider: str) -> str:
        s = self._settings
        if provider == "gemini":
            return s.gemini_model_chat if task == "ask_relationship" else s.gemini_model_fast
        if provider == "dashscope":
            if task == "parse_screenshot":
                return s.dashscope_vision_model
            if task == "transcribe":
                return s.dashscope_asr_model
            return s.dashscope_extract_model
        if provider == "ark":
            return s.ark_chat_model
        if provider == "deepseek":
            return s.deepseek_chat_model

        spec = self._routing.tasks.get(task)
        if spec and provider in spec.models:
            return spec.models[provider]
        return f"{provider}-default"

    def _resolve_timeout(self, task: str) -> float:
        if task == "safety_classify":
            return self._settings.safety_timeout_seconds
        return self._settings.ai_timeout_seconds

    async def _call_adapter_with_retries(
        self,
        adapter: ProviderAdapter,
        breaker: CircuitBreaker,
        request: AdapterCallRequest,
    ) -> AdapterCallResponse:
        self._assert_provider_allowed(adapter.provider_name)

        def _on_before_sleep(_retry_state: Any) -> None:
            breaker.record_failure()

        retrying = AsyncRetrying(
            stop=stop_after_attempt(max(1, self._routing.retry_max_attempts)),
            wait=wait_exponential_jitter(
                initial=self._routing.retry_initial_wait_seconds,
                max=self._routing.retry_max_wait_seconds,
                jitter=self._routing.retry_jitter_seconds,
            ),
            retry=retry_if_exception_type(AdapterTransientError),
            before_sleep=_on_before_sleep,
            reraise=True,
        )

        try:
            async for attempt in retrying:
                with attempt:
                    resp = await adapter.complete(request)
                    breaker.record_success()
                    return resp
        except AdapterTransientError:
            # Final exhausted attempt
            breaker.record_failure()
            raise

        raise UpstreamAIError("Unreachable retry exhaustion state")

    async def _execute_structured(
        self,
        *,
        scope: UserScope,
        task: str,
        rendered_prompt: RenderedPrompt,
        schema_cls: type[SchemaT],
        structured_context: dict[str, Any],
        media_bytes: bytes | None = None,
        media_mime_type: str | None = None,
        session: AsyncSession | None = None,
    ) -> tuple[SchemaT, str, str]:
        spec = self._routing.tasks[task]
        timeout_sec = self._resolve_timeout(task)
        last_error_code = "all_providers_failed"

        for provider_name in self._provider_chain_for_task(task):
            adapter = self._adapters.get(provider_name)
            if adapter is None:
                continue
            breaker = self._breakers[provider_name]
            model_name = self._resolve_model(task, provider_name)

            if not breaker.allow_request():
                await self._meter.record(
                    scope=scope,
                    task=task,
                    provider=provider_name,
                    model=model_name,
                    prompt_id=rendered_prompt.prompt_id,
                    prompt_version=rendered_prompt.version,
                    prompt_hash=rendered_prompt.sha256,
                    input_tokens=0,
                    output_tokens=0,
                    latency_ms=0,
                    outcome="circuit_open",
                    session=session,
                )
                last_error_code = "circuit_open"
                continue

            req = AdapterCallRequest(
                task=task,
                model=model_name,
                prompt=rendered_prompt.rendered_text,
                temperature=spec.temperature,
                timeout_seconds=timeout_sec,
                response_schema=schema_cls,
                media_bytes=media_bytes,
                media_mime_type=media_mime_type,
                structured_context=structured_context,
            )

            t0 = time.perf_counter()
            try:
                resp = await self._call_adapter_with_retries(adapter, breaker, req)
            except (AdapterError, TimeoutError) as exc:
                lat_ms = int((time.perf_counter() - t0) * 1000)
                outcome = "timeout" if isinstance(exc, TimeoutError) else "upstream_error"
                await self._meter.record(
                    scope=scope,
                    task=task,
                    provider=provider_name,
                    model=model_name,
                    prompt_id=rendered_prompt.prompt_id,
                    prompt_version=rendered_prompt.version,
                    prompt_hash=rendered_prompt.sha256,
                    input_tokens=estimate_token_count(rendered_prompt.rendered_text),
                    output_tokens=0,
                    latency_ms=lat_ms,
                    outcome=outcome,
                    session=session,
                )
                logger.warning(
                    "ai_provider_call_failed",
                    user_id=str(scope.user_id),
                    task=task,
                    provider=provider_name,
                    model=model_name,
                    error_type=type(exc).__name__,
                )
                last_error_code = outcome
                continue

            lat_ms = int((time.perf_counter() - t0) * 1000)
            try:
                parsed = parse_and_validate_json(resp.text, schema_cls)
                await self._meter.record(
                    scope=scope,
                    task=task,
                    provider=provider_name,
                    model=resp.model,
                    prompt_id=rendered_prompt.prompt_id,
                    prompt_version=rendered_prompt.version,
                    prompt_hash=rendered_prompt.sha256,
                    input_tokens=resp.input_tokens,
                    output_tokens=resp.output_tokens,
                    latency_ms=lat_ms,
                    outcome="ok",
                    session=session,
                )
                return (
                    parsed,
                    rendered_prompt.version_tag,
                    f"{provider_name}:{resp.model}",
                )
            except StructuredOutputValidationError as val_err:
                await self._meter.record(
                    scope=scope,
                    task=task,
                    provider=provider_name,
                    model=resp.model,
                    prompt_id=rendered_prompt.prompt_id,
                    prompt_version=rendered_prompt.version,
                    prompt_hash=rendered_prompt.sha256,
                    input_tokens=resp.input_tokens,
                    output_tokens=resp.output_tokens,
                    latency_ms=lat_ms,
                    outcome="validation_failed",
                    session=session,
                )

                # 1-Shot JSON Repair Retry on same provider
                repair_prompt_str = build_json_repair_prompt(
                    broken_output=resp.text,
                    schema_cls=schema_cls,
                    validation_error=val_err,
                )
                repair_req = AdapterCallRequest(
                    task=task,
                    model=model_name,
                    prompt=repair_prompt_str,
                    temperature=0.0,
                    timeout_seconds=timeout_sec,
                    response_schema=schema_cls,
                    structured_context=structured_context,
                )
                t_rep = time.perf_counter()
                try:
                    rep_resp = await self._call_adapter_with_retries(adapter, breaker, repair_req)
                    rep_lat = int((time.perf_counter() - t_rep) * 1000)
                    repaired = parse_and_validate_json(rep_resp.text, schema_cls)
                    await self._meter.record(
                        scope=scope,
                        task=task,
                        provider=provider_name,
                        model=rep_resp.model,
                        prompt_id=rendered_prompt.prompt_id,
                        prompt_version=rendered_prompt.version,
                        prompt_hash=rendered_prompt.sha256,
                        input_tokens=rep_resp.input_tokens,
                        output_tokens=rep_resp.output_tokens,
                        latency_ms=rep_lat,
                        outcome="repaired_ok",
                        session=session,
                    )
                    return (
                        repaired,
                        rendered_prompt.version_tag,
                        f"{provider_name}:{rep_resp.model}",
                    )
                except UpstreamAIError:
                    raise
                except Exception as rep_exc:
                    rep_lat = int((time.perf_counter() - t_rep) * 1000)
                    await self._meter.record(
                        scope=scope,
                        task=task,
                        provider=provider_name,
                        model=model_name,
                        prompt_id=rendered_prompt.prompt_id,
                        prompt_version=rendered_prompt.version,
                        prompt_hash=rendered_prompt.sha256,
                        input_tokens=estimate_token_count(repair_prompt_str),
                        output_tokens=0,
                        latency_ms=rep_lat,
                        outcome="repair_failed",
                        session=session,
                    )
                    logger.warning(
                        "ai_json_repair_failed",
                        user_id=str(scope.user_id),
                        task=task,
                        provider=provider_name,
                        error_type=type(rep_exc).__name__,
                    )
                    last_error_code = "repair_failed"
                    continue

        raise UpstreamAIError(
            f"AI task '{task}' failed across configured providers ({last_error_code})",
            extensions={"reason": last_error_code, "task": task},
        )

    # -----------------------------------------------------------------------
    # ModelGatewayPort public methods
    # -----------------------------------------------------------------------

    async def transcribe(
        self,
        *,
        scope: UserScope,
        payload: TranscribeInput,
        session: AsyncSession | None = None,
    ) -> TranscribeOutput:
        masked_names = [mask_hard_pii(n).masked_text for n in payload.known_person_names]
        masked_active = (
            mask_hard_pii(payload.active_person_name).masked_text
            if payload.active_person_name
            else None
        )
        masked_snippets = [mask_hard_pii(s).masked_text for s in payload.recent_context_snippets]
        masked_payload = payload.model_copy(
            update={
                "known_person_names": masked_names,
                "active_person_name": masked_active,
                "recent_context_snippets": masked_snippets,
            }
        )
        rendered = self._prompts.render(
            "transcribe_audio",
            language=masked_payload.language,
            active_person_name=masked_active,
            known_person_names_json=masked_names,
            recent_context_json=masked_snippets,
        )
        out, _, _ = await self._execute_structured(
            scope=scope,
            task="transcribe",
            rendered_prompt=rendered,
            schema_cls=TranscribeOutput,
            structured_context={"payload": masked_payload.model_dump()},
            media_bytes=masked_payload.audio_bytes,
            media_mime_type=masked_payload.mime_type,
            session=session,
        )
        raw_clean = out.raw_transcript.strip()
        corr_seed = out.corrected_transcript.strip()
        if not raw_clean and not corr_seed:
            return out.model_copy(update={"raw_transcript": "", "corrected_transcript": ""})
        polished = postprocess_asr_transcript(
            corr_seed or raw_clean,
            active_person_name=masked_active,
            known_person_names=masked_names,
            recent_context_snippets=masked_snippets,
        )
        return out.model_copy(
            update={
                "raw_transcript": raw_clean,
                "corrected_transcript": polished,
            }
        )

    async def parse_screenshot(
        self,
        *,
        scope: UserScope,
        image_bytes: bytes,
        mime_type: str,
        target_person_name: str | None = None,
        session: AsyncSession | None = None,
    ) -> ScreenshotParseOutput:
        masked_target = (
            mask_hard_pii(target_person_name).masked_text if target_person_name else None
        )
        rendered = self._prompts.render(
            "parse_screenshot",
            target_person_name=masked_target,
        )
        out, _, _ = await self._execute_structured(
            scope=scope,
            task="parse_screenshot",
            rendered_prompt=rendered,
            schema_cls=ScreenshotParseOutput,
            structured_context={"target_person_name": masked_target},
            media_bytes=image_bytes,
            media_mime_type=mime_type,
            session=session,
        )
        return out

    async def extract_record(
        self,
        *,
        scope: UserScope,
        payload: ExtractRecordInput,
        session: AsyncSession | None = None,
    ) -> tuple[ExtractRecordOutput, str, str]:
        masked_payload = _mask_extract_payload(payload)
        known_json = [
            {
                "person_id": str(p.person_id),
                "display_name": p.display_name,
                "aliases": p.aliases,
                "tier": p.tier.value,
                "role_tag": p.role_tag,
                "recent_facts": p.recent_facts[:6],
            }
            for p in masked_payload.known_persons
        ]
        rendered = self._prompts.render(
            "extract_record",
            current_date=masked_payload.current_date,
            route_mode=masked_payload.route_mode,
            input_source=masked_payload.input_source,
            scoped_person_id=(
                str(masked_payload.scoped_person_id)
                if masked_payload.scoped_person_id is not None
                else None
            ),
            scoped_person_tier=(
                masked_payload.scoped_person_tier.value
                if masked_payload.scoped_person_tier is not None
                else None
            ),
            known_persons_json=known_json,
            recent_history_json=masked_payload.recent_history_hints,
            user_input=masked_payload.text,
        )
        raw_out, prompt_ver, model_profile = await self._execute_structured(
            scope=scope,
            task="extract_record",
            rendered_prompt=rendered,
            schema_cls=ExtractRecordOutput,
            structured_context={"payload": masked_payload.model_dump(mode="json")},
            session=session,
        )
        sanitized = _sanitize_extract_output(raw_out, masked_payload)
        return sanitized, prompt_ver, model_profile

    async def generate_companion_reply(
        self,
        *,
        scope: UserScope,
        payload: CompanionChatInput,
        session: AsyncSession | None = None,
    ) -> tuple[CompanionChatOutput, str, str]:
        masked_payload = _mask_companion_payload(payload)
        rendered = self._render_companion_prompt(masked_payload)
        raw_out, prompt_ver, model_profile = await self._execute_structured(
            scope=scope,
            task="ask_relationship",
            rendered_prompt=rendered,
            schema_cls=CompanionChatOutput,
            structured_context={"payload": masked_payload.model_dump(mode="json")},
            session=session,
        )
        allowed_ids = self._allowed_record_ids(masked_payload)
        verified_out = enforce_companion_citations(raw_out, allowed_ids)
        guarded_out = enforce_companion_output_guard(verified_out, tier=masked_payload.tier)
        return guarded_out, prompt_ver, model_profile

    async def stream_companion_reply(
        self,
        *,
        scope: UserScope,
        payload: CompanionChatInput,
        session: AsyncSession | None = None,
    ) -> AsyncIterator[str]:
        masked_payload = _mask_companion_payload(payload)
        rendered = self._render_companion_prompt(masked_payload)
        spec = self._routing.tasks["ask_relationship"]
        timeout_sec = self._resolve_timeout("ask_relationship")
        allowed_ids = self._allowed_record_ids(masked_payload)

        last_err: Exception | None = None
        for provider_name in self._provider_chain():
            self._assert_provider_allowed(provider_name)
            adapter = self._adapters.get(provider_name)
            if adapter is None:
                continue
            self._assert_provider_allowed(adapter.provider_name)
            breaker = self._breakers[provider_name]
            model_name = self._resolve_model("ask_relationship", provider_name)
            if not breaker.allow_request():
                continue

            req = AdapterCallRequest(
                task="ask_relationship",
                model=model_name,
                prompt=rendered.rendered_text,
                temperature=spec.temperature,
                timeout_seconds=timeout_sec,
                structured_context={"payload": masked_payload.model_dump(mode="json")},
            )
            processor = StreamingCitationProcessor(allowed_record_ids=allowed_ids)
            emitted_chars = 0
            t0 = time.perf_counter()
            try:
                async for raw_chunk in adapter.stream(req):
                    safe_chunk = processor.feed(raw_chunk)
                    if safe_chunk:
                        emitted_chars += len(safe_chunk)
                        yield safe_chunk
                tail = processor.flush()
                if tail:
                    emitted_chars += len(tail)
                    yield tail

                breaker.record_success()
                lat_ms = int((time.perf_counter() - t0) * 1000)
                await self._meter.record(
                    scope=scope,
                    task="ask_relationship",
                    provider=provider_name,
                    model=model_name,
                    prompt_id=rendered.prompt_id,
                    prompt_version=rendered.version,
                    prompt_hash=rendered.sha256,
                    input_tokens=estimate_token_count(rendered.rendered_text),
                    output_tokens=max(1, (emitted_chars + 1) // 2),
                    latency_ms=lat_ms,
                    outcome="stream_ok",
                    session=session,
                )
                return
            except UpstreamAIError:
                raise
            except Exception as exc:
                last_err = exc
                breaker.record_failure()
                lat_ms = int((time.perf_counter() - t0) * 1000)
                await self._meter.record(
                    scope=scope,
                    task="ask_relationship",
                    provider=provider_name,
                    model=model_name,
                    prompt_id=rendered.prompt_id,
                    prompt_version=rendered.version,
                    prompt_hash=rendered.sha256,
                    input_tokens=estimate_token_count(rendered.rendered_text),
                    output_tokens=max(0, emitted_chars // 2),
                    latency_ms=lat_ms,
                    outcome="stream_error",
                    session=session,
                )
                if emitted_chars > 0:
                    # Cannot seamlessly restart mid-stream on fallback if bytes already yielded
                    break

        raise UpstreamAIError(
            "Streaming companion reply failed across configured providers"
        ) from last_err

    async def classify_safety(
        self,
        *,
        scope: UserScope,
        text: str,
        session: AsyncSession | None = None,
    ) -> SafetyClassifyOutput:
        masked_text = mask_hard_pii(text).masked_text
        rendered = self._prompts.render("safety_classify", user_text=masked_text)
        out, _, _ = await self._execute_structured(
            scope=scope,
            task="safety_classify",
            rendered_prompt=rendered,
            schema_cls=SafetyClassifyOutput,
            structured_context={"text": masked_text},
            session=session,
        )
        return out

    async def summarize_thread(
        self,
        *,
        scope: UserScope,
        person_name: str,
        previous_summary: str | None,
        turns_text: str,
        session: AsyncSession | None = None,
    ) -> str:
        masked_prev = mask_hard_pii(previous_summary).masked_text if previous_summary else None
        masked_turns = mask_hard_pii(turns_text).masked_text
        rendered = self._prompts.render(
            "summarize_thread",
            person_name=person_name,
            previous_summary=masked_prev,
            turns_text=masked_turns,
        )
        spec = self._routing.tasks["summarize_thread"]
        timeout_sec = self._resolve_timeout("summarize_thread")

        for provider_name in self._provider_chain():
            self._assert_provider_allowed(provider_name)
            adapter = self._adapters.get(provider_name)
            if adapter is None:
                continue
            self._assert_provider_allowed(adapter.provider_name)
            breaker = self._breakers[provider_name]
            model_name = self._resolve_model("summarize_thread", provider_name)
            if not breaker.allow_request():
                continue
            req = AdapterCallRequest(
                task="summarize_thread",
                model=model_name,
                prompt=rendered.rendered_text,
                temperature=spec.temperature,
                timeout_seconds=timeout_sec,
                structured_context={
                    "person_name": person_name,
                    "previous_summary": masked_prev,
                    "turns_text": masked_turns,
                },
            )
            t0 = time.perf_counter()
            try:
                resp = await self._call_adapter_with_retries(adapter, breaker, req)
                lat_ms = int((time.perf_counter() - t0) * 1000)
                await self._meter.record(
                    scope=scope,
                    task="summarize_thread",
                    provider=provider_name,
                    model=resp.model,
                    prompt_id=rendered.prompt_id,
                    prompt_version=rendered.version,
                    prompt_hash=rendered.sha256,
                    input_tokens=resp.input_tokens,
                    output_tokens=resp.output_tokens,
                    latency_ms=lat_ms,
                    outcome="ok",
                    session=session,
                )
                return resp.text.strip()
            except UpstreamAIError:
                raise
            except Exception as exc:
                lat_ms = int((time.perf_counter() - t0) * 1000)
                await self._meter.record(
                    scope=scope,
                    task="summarize_thread",
                    provider=provider_name,
                    model=model_name,
                    prompt_id=rendered.prompt_id,
                    prompt_version=rendered.version,
                    prompt_hash=rendered.sha256,
                    input_tokens=estimate_token_count(rendered.rendered_text),
                    output_tokens=0,
                    latency_ms=lat_ms,
                    outcome="upstream_error",
                    session=session,
                )
                logger.warning(
                    "ai_summarize_failed",
                    user_id=str(scope.user_id),
                    provider=provider_name,
                    error_type=type(exc).__name__,
                )
                continue

        raise UpstreamAIError("Thread summarization failed across configured providers")

    def _render_companion_prompt(self, masked_payload: CompanionChatInput) -> RenderedPrompt:
        person_obj = {
            "person_id": str(masked_payload.person_id),
            "display_name": masked_payload.person_name,
            "tier": masked_payload.tier.value,
            "role_tag": masked_payload.role_tag,
            "traits": masked_payload.traits,
        }
        facts_arr = [
            {
                "record_id": str(f.record_id),
                "fact_id": str(f.fact_id),
                "fact_type": f.fact_type.value,
                "content": f.content,
                "happened_on": f.happened_on,
            }
            for f in masked_payload.confirmed_facts
        ]
        mats_arr = [
            {
                "record_id": str(m.record_id),
                "kind": m.kind,
                "title": m.title,
                "body": m.body,
                "happened_on": m.happened_on,
            }
            for m in masked_payload.referenced_materials
        ]
        turns_arr = [{"role": t.role, "content": t.content} for t in masked_payload.recent_turns]
        return self._prompts.render(
            "ask_relationship",
            person_json=person_obj,
            confirmed_facts_json=facts_arr,
            referenced_materials_json=mats_arr,
            rolling_summary=masked_payload.rolling_summary,
            recent_turns_json=turns_arr,
            safety_override=masked_payload.safety_override.value,
            user_query=masked_payload.user_message,
        )

    @staticmethod
    def _allowed_record_ids(payload: CompanionChatInput) -> set[UUID]:
        allowed = {f.record_id for f in payload.confirmed_facts}
        allowed.update(m.record_id for m in payload.referenced_materials)
        return allowed
