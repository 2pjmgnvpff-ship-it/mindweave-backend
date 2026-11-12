"""Independent dual-layer safety evaluation service (L1 rules + L2 LLM classifier, Invariant I5)."""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.ai.ports import ModelGatewayPort, SafetyServicePort
from mindweave.ai.schemas.contracts import (
    CrisisCardPayload,
    SafetyClassifyOutput,
    SafetyDecision,
)
from mindweave.core.logging import get_logger
from mindweave.modules.safety.domain.l1_rules import L1SafetyEngine, L1SafetyResult
from mindweave.modules.safety.domain.templates import (
    AuditedCrisisTemplate,
    get_crisis_template_for_category,
)
from mindweave.shared.enums import SafetyCategory
from mindweave.shared.scopes import UserScope

logger = get_logger(__name__)

_SEVERITY_RANK: dict[SafetyCategory, int] = {
    SafetyCategory.NORMAL: 0,
    SafetyCategory.BOUNDARY_REFUSAL: 20,
    SafetyCategory.THIRD_PARTY_ATTACK: 25,
    SafetyCategory.ILLEGAL_INTENT: 30,
    SafetyCategory.VIOLENCE_VICTIM: 40,
    SafetyCategory.CRISIS: 50,
}


def template_to_payload(template: AuditedCrisisTemplate) -> CrisisCardPayload:
    return CrisisCardPayload(
        template_id=template.template_id,
        title=template.title,
        care_message=template.care_message,
        hotlines=template.hotlines_as_dicts(),
        disclaimer=template.disclaimer,
    )


def merge_l1_and_l2(l1: L1SafetyResult, l2: SafetyClassifyOutput) -> tuple[SafetyCategory, str]:
    """Deterministically combine L1 rule result and L2 semantic classifier result."""
    l2_cat = SafetyCategory.NORMAL if l2.is_hyperbole_or_joke else l2.category

    if l1.category is SafetyCategory.NORMAL and l2_cat is SafetyCategory.NORMAL:
        return SafetyCategory.NORMAL, "merged"

    if l1.category is not SafetyCategory.NORMAL and l2_cat is SafetyCategory.NORMAL:
        return l1.category, "l1"

    if l1.category is SafetyCategory.NORMAL and l2_cat is not SafetyCategory.NORMAL:
        # Guard against L2 false-positive crisis lock on clear colloquial hyperbole
        if l1.is_hyperbole_excluded and l2_cat is SafetyCategory.CRISIS:
            return SafetyCategory.NORMAL, "l1"
        return l2_cat, "l2"

    # Both L1 and L2 triggered non-normal categories -> pick highest severity
    if _SEVERITY_RANK[l2_cat] > _SEVERITY_RANK[l1.category]:
        return l2_cat, "merged"
    return l1.category, "merged"


def build_safety_decision(category: SafetyCategory, *, trigger_source: str) -> SafetyDecision:
    tpl = get_crisis_template_for_category(category)
    is_life_safety = category in (SafetyCategory.CRISIS, SafetyCategory.VIOLENCE_VICTIM)
    return SafetyDecision(
        category=category,
        block_normal_reply=is_life_safety,
        crisis_card=template_to_payload(tpl) if tpl is not None else None,
        trigger_source=trigger_source,
    )


class SafetyService(SafetyServicePort):
    """Dual-layer independent safety subsystem with timeout fail-safe (Invariant I5)."""

    def __init__(
        self,
        gateway: ModelGatewayPort,
        *,
        l1_engine: L1SafetyEngine | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._gateway = gateway
        self._l1 = l1_engine or L1SafetyEngine()
        self._timeout_seconds = max(0.05, timeout_seconds)

    @property
    def l1_engine(self) -> L1SafetyEngine:
        return self._l1

    async def evaluate(
        self,
        *,
        scope: UserScope,
        text: str,
        session: AsyncSession | None = None,
    ) -> SafetyDecision:
        l1_res = self._l1.evaluate(text)

        if not (text or "").strip():
            return build_safety_decision(SafetyCategory.NORMAL, trigger_source="l1")

        try:
            l2_res = await asyncio.wait_for(
                self._gateway.classify_safety(
                    scope=scope,
                    text=text,
                    session=session,
                ),
                timeout=self._timeout_seconds,
            )
        except Exception as exc:
            # Fail-safe (Master Plan §3.5): never drop user data or crash when L2 times out/errors;
            # fall back cleanly to L1 with zero-PII structured telemetry.
            logger.warning(
                "safety_l2_fallback_to_l1",
                user_id=str(scope.user_id),
                l1_category=l1_res.category.value,
                error_type=type(exc).__name__,
            )
            return build_safety_decision(l1_res.category, trigger_source="l1")

        merged_category, source = merge_l1_and_l2(l1_res, l2_res)
        if merged_category in (SafetyCategory.CRISIS, SafetyCategory.VIOLENCE_VICTIM):
            logger.info(
                "crisis_flow_entered",
                user_id=str(scope.user_id),
                trigger_category=merged_category.value,
                trigger_source=source,
            )
        return build_safety_decision(merged_category, trigger_source=source)
