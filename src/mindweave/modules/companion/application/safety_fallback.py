"""Deterministic SOP v0.2 L1 Safety fallback when no external `SafetyServicePort` is wired."""

from __future__ import annotations

import re

from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.ai.schemas.contracts import SafetyDecision
from mindweave.modules.companion.domain.tier_policy import (
    CRISIS_SELF_HARM_CARD_V1,
    DOMESTIC_VIOLENCE_CARD_V1,
)
from mindweave.shared.enums import SafetyCategory
from mindweave.shared.scopes import UserScope

# RT-09 everyday work/study hyperbole exclusions (prevent false-positive lockouts)
_HYPERBOLE_WORK_CONTEXT_RE = re.compile(
    r"(?:改\s*PPT|写论文|加班|需求|甲方的?稿|周报|开会|早八|堵车|打游戏|排位|连跪).{0,12}(?:想死|原地去世|要命|不想活)"
    r"|(?:想死|原地去世).{0,8}(?:哈哈|笑死|救命啊哈哈|呜呜哈哈)",
    re.IGNORECASE,
)

# Explicit & implicit self-harm / suicide crisis patterns (SOP §2.1 & RT-07/RT-08)
_CRISIS_SELF_HARM_RE = re.compile(
    r"(?:"
    r"(?:不想活了|活着.{0,6}没有意义|活着没意思|不如死了|想结束生命|想轻生|想自杀|准备自杀)"
    r"|(?:吞药|吃下?.{0,4}安眠药|攒够了.{0,6}药|睡.{0,4}永远不醒|再也不醒来)"
    r"|(?:跳楼|跳河|割腕|烧炭|上吊|遗书|留遗言)"
    r"|(?:最后一眼.{0,12}(?:走|离开|解脱)|(?:托付|照顾好).{0,8}(?:猫|狗|宠物).{0,14}(?:密码|彻底解脱|离开这个世界))"
    r")",
    re.IGNORECASE,
)

# Domestic violence / immediate physical abuse victim patterns (SOP §2.1 & §2.2 Scene B)
_VIOLENCE_VICTIM_RE = re.compile(
    r"(?:"
    r"(?:正在|又|刚刚)?.{0,4}(?:家暴我|殴打我|掐我脖子|拿刀威胁我|把我锁在屋里|限制我人身自由|动手打我)"
    r"|(?:遭遇|遭受).{0,6}(?:家庭暴力|家暴|人身侵害|虐待)"
    r")",
    re.IGNORECASE,
)

# Concrete offline illegal harm intent (SOP §1.2 Boundary 2 — distinct from verbal venting)
_ILLEGAL_INTENT_RE = re.compile(
    r"(?:"
    r"(?:下药|放.{0,4}百草枯|投毒|剪.{0,4}刹车)"
    r"|(?:买了?把?刀|带把?刀|拿刀去).{0,12}(?:堵[他她]|捅[他她]|砍[他她]|弄[他她])"
    r"|(?:把[他她]的?.{0,6}(?:私密照|裸照|不雅视频).{0,10}(?:发到|群发|曝光|散布))"
    r"|(?:敲诈[他她]|勒索[他她]|找人去揍[他她]|雇人打[他她])"
    r")",
    re.IGNORECASE,
)

# Third-party scoring / lie-detection / clinical diagnosis requests (Not-To-Do §1.1)
_THIRD_PARTY_ATTACK_RE = re.compile(
    r"(?:测谎仪|欺骗指数|渣男分|渣女分|海王概率|打个?信用分|言行一致性测谎|是不是\s*NPD|人格障碍鉴定)",
    re.IGNORECASE,
)


class BuiltinSafetyFallback:
    """Audited deterministic L1 `SafetyServicePort` fallback."""

    async def evaluate(
        self,
        *,
        scope: UserScope,
        text: str,
        session: AsyncSession | None = None,
    ) -> SafetyDecision:
        del scope, session
        cleaned = (text or "").strip()
        if not cleaned:
            return SafetyDecision(category=SafetyCategory.NORMAL, trigger_source="l1")

        if _VIOLENCE_VICTIM_RE.search(cleaned):
            return SafetyDecision(
                category=SafetyCategory.VIOLENCE_VICTIM,
                block_normal_reply=True,
                crisis_card=DOMESTIC_VIOLENCE_CARD_V1,
                trigger_source="l1",
            )

        if _ILLEGAL_INTENT_RE.search(cleaned):
            return SafetyDecision(
                category=SafetyCategory.ILLEGAL_INTENT,
                block_normal_reply=False,
                trigger_source="l1",
            )

        if _CRISIS_SELF_HARM_RE.search(cleaned) and not _HYPERBOLE_WORK_CONTEXT_RE.search(cleaned):
            return SafetyDecision(
                category=SafetyCategory.CRISIS,
                block_normal_reply=True,
                crisis_card=CRISIS_SELF_HARM_CARD_V1,
                trigger_source="l1",
            )

        # Even if work words appear, self-directed lethal method preparation always triggers crisis
        if re.search(
            r"(?:(?:喝|吞|吃).{0,6}(?:安眠药|百草枯)|(?:准备|打算|今晚|决定).{0,6}(?:跳楼|割腕|烧炭|写遗书))",
            cleaned,
        ):
            return SafetyDecision(
                category=SafetyCategory.CRISIS,
                block_normal_reply=True,
                crisis_card=CRISIS_SELF_HARM_CARD_V1,
                trigger_source="l1",
            )

        if _THIRD_PARTY_ATTACK_RE.search(cleaned):
            return SafetyDecision(
                category=SafetyCategory.THIRD_PARTY_ATTACK,
                block_normal_reply=False,
                trigger_source="l1",
            )

        return SafetyDecision(category=SafetyCategory.NORMAL, trigger_source="l1")
