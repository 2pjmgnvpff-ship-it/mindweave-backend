"""Three-Tier Cupid Companion Policies, Crisis Cards, Session Care & Write-Back Gate (Pure Domain).

Pure Python domain module — zero framework, database, or vendor SDK imports.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from mindweave.ai.schemas.contracts import (
    CompanionChatInput,
    CompanionChatOutput,
    CrisisCardPayload,
)
from mindweave.shared.enums import (
    FactStatus,
    FactType,
    PendingReason,
    PersonState,
    SafetyCategory,
    SourceSpeaker,
    Tier,
)

# ============================================================================
# Audited Fixed Crisis Card Templates (Crisis SOP v0.2 §2.2 — AI MUST NOT rewrite)
# ============================================================================

CRISIS_SELF_HARM_CARD_V1 = CrisisCardPayload(
    template_id="crisis_self_harm_v1",
    title="心络温暖守护提示：此刻请先照顾好你自己",
    care_message=(
        "看到你写下的这些话，我们非常担心你现在的状态。"
        "你正在经历的痛苦和委屈是真实的，但请一定不要一个人扛着。"
        "作为一款私密手账工具，AI 无法替代专业人士和现实中在乎你的人给你真正的支持。"
        "请立刻停下来，联系以下 24 小时免费专业支持渠道，或者给你身边信任的家人、朋友打个电话。"
    ),
    hotlines=[
        {
            "name": "全国统一心理援助热线",
            "phone": "12356",
            "note": "卫健委设立，24小时心理疏导与危机干预",
        },
        {
            "name": "希望24热线",
            "phone": "400-161-9995",
            "note": "全国24小时免费心理危机干预",
        },
        {"name": "紧急医疗救援电话", "phone": "120", "note": "24小时紧急医疗救助"},
        {"name": "紧急报警求助电话", "phone": "110", "note": "24小时紧急警务求助"},
    ],
    disclaimer="当前已停止常规对话。AI 无法替代专业心理危机干预与医疗救助，请立即寻求专业帮助。",
)

DOMESTIC_VIOLENCE_CARD_V1 = CrisisCardPayload(
    template_id="domestic_violence_v1",
    title="安全求助提示：你的人身安全高于一切",
    care_message=(
        "如果你正在遭受暴力对待、威胁或人身限制，请优先确保自己处于安全的环境中，"
        "并及时向警方或专业机构求助。"
    ),
    hotlines=[
        {
            "name": "紧急报警电话",
            "phone": "110",
            "note": "不便语音时可发送位置短信至 12110 短信报警",
        },
        {"name": "全国妇联妇女维权公益服务热线", "phone": "12338", "note": "妇女维权与反家暴求助"},
        {"name": "全国统一心理援助热线", "phone": "12356", "note": "24小时心理疏导与危机干预"},
    ],
    disclaimer="你的人身安全高于一切，请优先确保自身安全并向警方或专业维权机构求助。",
)


def default_crisis_card_for(category: SafetyCategory) -> CrisisCardPayload:
    if category is SafetyCategory.VIOLENCE_VICTIM:
        return DOMESTIC_VIOLENCE_CARD_V1
    return CRISIS_SELF_HARM_CARD_V1


# ============================================================================
# Crisis SOP v0.2 §1.2 & §3.3 Canonical Protective Copy
# ============================================================================

ILLEGAL_INTENT_ALLY_PULLBACK = (
    "我完全懂你现在有多难受、多委屈，但千万别冲动做伤害身体或违法的事——"
    "为了这种人搭上你自己的前途太不值了。"
    "我们已经把过往客观记录都封存好了，咱们用合法留痕反击、正式举证或书面申诉的方式，"
    "既能维护自身权益，又能稳妥保护好你自己。"
)

PUBLIC_SHAMING_BOUNDARY_GUIDANCE = (
    "你现在感到委屈和生气完全正常，这里只属于你，先按时间冷静留存客观事实再一步步处理，"
    "但我不能帮你生成发朋友圈、小红书或大群的实名挂人曝光卡片——"
    "一旦公开传播对方真名和职务，反而可能让你自己惹上名誉权侵权的麻烦，太不划算了。"
    "不如我帮你起草一份《职场书面留痕回复》或《客观事实时间线清单》，体面又有力地守住边界。"
)

THIRD_PARTY_SCORING_REFUSAL = (
    "我只能帮你客观回顾手帐里记录过的真实片段，无法对他人做性格定性、测谎或打分。"
)

THIRD_PARTY_DISCLAIMER = (
    "心络仅基于您记录的客观片段协助梳理沟通思路，不提供第三方人格评判、测谎或信用打分。"
)

LATE_NIGHT_REST_WHISPER_SILVER = (
    "今天受了这么多委屈，先慢慢缓一缓。现在夜深了，先好好睡一觉养足精神，"
    "你的手帐我都帮你收好了，明天醒来我们再看下一步。"
)

LATE_NIGHT_REST_WHISPER_DEFAULT = (
    "现在夜深了，先好好睡一觉养足精神，你的手帐我都帮你收好了，明天醒来我们再看下一步。"
)


# ============================================================================
# 3-Tier Cupid Policy Specification
# ============================================================================


@dataclass(frozen=True, slots=True)
class TierPolicySpec:
    tier: Tier
    label_cn: str
    allows_perspectives: bool
    allows_public_share_card: bool
    system_prompt_fragment: str


TIER_POLICIES: dict[Tier, TierPolicySpec] = {
    Tier.GOLD: TierPolicySpec(
        tier=Tier.GOLD,
        label_cn="金箭 · 珍重",
        allows_perspectives=True,
        allows_public_share_card=False,
        system_prompt_fragment=(
            "【金箭 · 珍重策略】先细腻共情安抚情绪，调取已确认的过往偏好/付出细节（附 [S1]）；"
            "在 alternative_perspectives 中提供温和不强加的换位思考供前端折叠卡展示，"
            "在 actionable_suggestions 中给出自然得体的沟通或行动草稿（由用户自主复制发送，绝不代发）。"
            "严禁给感情打分或测谎。"
        ),
    ),
    Tier.SILVER: TierPolicySpec(
        tier=Tier.SILVER,
        label_cn="银盾 · 边界",
        allows_perspectives=False,
        allows_public_share_card=False,
        system_prompt_fragment=(
            "【银盾 · 边界策略】坚定站在用户立场，冷静留存客观时间线与书面留痕，绝不劝和，绝不替对方开脱。"
            "alternative_perspectives 必须强制返回空数组 []。"
            "按时间线条理清晰地梳理已记录的越界/甩锅客观事实（附 [S1]），"
            "情绪站用户这边、事实保持诚实：只复述已记录事实，绝不为附和而夸大或编造；"
            "若用户此刻说法与其本人之前的记录矛盾，平静地原样提一句那条记录（附 [S1]），"
            "不评判、不追问、不借此劝和说教。"
            "可起草体面有力的职场书面留痕回复或边界防雷话术；"
            "严禁生成发朋友圈/小红书的公网挂人曝光长图；若遇线下违法冲动，温和拉住并引导合法留痕维权。"
        ),
    ),
    Tier.CRYSTAL: TierPolicySpec(
        tier=Tier.CRYSTAL,
        label_cn="水晶 · 日常",
        allows_perspectives=False,
        allows_public_share_card=False,
        system_prompt_fragment=(
            "【水晶 · 日常策略】清爽干练、讲礼数与分寸。聚焦检索该对象的忌口、档期偏好、"
            "借还记录与待办承诺（附 [S1]），给出最省心不内耗的得体沟通话术。"
            "alternative_perspectives 强制返回空数组 []。"
        ),
    ),
}


def get_tier_policy(tier: Tier) -> TierPolicySpec:
    return TIER_POLICIES[tier]


_PUBLIC_SHAMING_RE = re.compile(
    r"(?:"
    r"(?:生成|做一张|写一篇|导出|发个?|做个?).{0,10}(?:挂人|曝光长图|罪状长图|反\s*PUA\s*鉴定书|网暴|大字报|黑料长图)"
    r"|(?:发到?|晒到?|挂到?|曝光到?).{0,6}(?:朋友圈|小红书|微博|抖音|全公司群|大群).{0,10}(?:挂[他她它]|骂[他她它]|曝光|社死|身败名裂)"
    r"|(?:人肉|开盒|公布.{0,4}(?:住址|手机号|身份证|私密照))"
    r")",
    re.IGNORECASE,
)

_ILLEGAL_SUGGESTION_RE = re.compile(
    r"(?:下药|百草枯|持刀|剪刹车|偷拍|私密照|裸照|敲诈|勒索|殴打|堵[他她]家)",
    re.IGNORECASE,
)


def is_public_shaming_request(user_message: str) -> bool:
    """Return True when the user asks to generate public shaming/doxxing/exposé material."""
    if not user_message:
        return False
    return bool(_PUBLIC_SHAMING_RE.search(user_message))


def apply_tier_policy(
    output: CompanionChatOutput,
    *,
    tier: Tier,
    user_message: str,
    safety_category: SafetyCategory = SafetyCategory.NORMAL,
) -> CompanionChatOutput:
    """Deterministically enforce 3-tier Cupid rules & SOP §1.2 boundaries on `CompanionChatOutput`.

    - ADR-005 #3: `Tier.SILVER` and `Tier.CRYSTAL` deterministically force `alternative_perspectives = []`.
    - Crisis SOP §1.2 Boundary 1: Public shaming / doxxing card requests are refused protectively.
    - Crisis SOP §1.2 Boundary 2: `SafetyCategory.ILLEGAL_INTENT` enforces protective ally pull-back
      ("为了这种人搭上你自己的前途太不值了…用合法留痕反击").
    - Not-To-Do §1.1: `SafetyCategory.THIRD_PARTY_ATTACK` enforces anti-scoring / anti-lie-detector disclaimer.
    """
    policy = get_tier_policy(tier)
    perspectives = list(output.alternative_perspectives) if policy.allows_perspectives else []
    suggestions = [s for s in output.actionable_suggestions if not _ILLEGAL_SUGGESTION_RE.search(s)]
    reply_text = output.reply_text.strip()
    whisper_note = output.whisper_note
    disclaimer = output.disclaimer
    effective_safety = (
        safety_category if safety_category is not SafetyCategory.NORMAL else output.safety_flag
    )

    shaming_requested = is_public_shaming_request(user_message)

    if effective_safety is SafetyCategory.ILLEGAL_INTENT:
        perspectives = []
        if "为了这种人搭上" not in reply_text and "合法留痕" not in reply_text:
            reply_text = (
                f"{ILLEGAL_INTENT_ALLY_PULLBACK}\n\n{reply_text}"
                if reply_text
                else ILLEGAL_INTENT_ALLY_PULLBACK
            )
        if not suggestions:
            suggestions = [
                "整理已记录的客观事实与时间线留痕清单",
                "起草正式书面沟通或合规申诉草稿，合法体面反击",
            ]
        effective_safety = SafetyCategory.ILLEGAL_INTENT

    elif shaming_requested:
        perspectives = []
        reply_text = PUBLIC_SHAMING_BOUNDARY_GUIDANCE
        suggestions = [
            "生成仅限自己私密查看的客观事实时间线清单",
            "起草不落把柄的职场书面留痕回复草稿",
        ]
        disclaimer = "为保护您免受名誉权纠纷风险，心络不支持生成对外公开传播的挂人或曝光内容。"
        effective_safety = SafetyCategory.BOUNDARY_REFUSAL

    elif effective_safety in (SafetyCategory.THIRD_PARTY_ATTACK, SafetyCategory.BOUNDARY_REFUSAL):
        if "无法对他人" not in reply_text and "打分" not in reply_text and "测谎" not in reply_text:
            reply_text = (
                f"{reply_text}\n\n{THIRD_PARTY_SCORING_REFUSAL}"
                if reply_text
                else THIRD_PARTY_SCORING_REFUSAL
            )
        if not disclaimer:
            disclaimer = THIRD_PARTY_DISCLAIMER

    return output.model_copy(
        update={
            "safety_flag": effective_safety,
            "reply_text": reply_text,
            "whisper_note": whisper_note,
            "alternative_perspectives": perspectives,
            "actionable_suggestions": suggestions,
            "disclaimer": disclaimer,
        }
    )


UNRECORDED_GAP_MARKERS: tuple[str, ...] = (
    "还没有记录",
    "尚未记录",
    "没有提到过",
    "手帐里还没有",
    "暂时没有记",
    "还没有记下",
    "还没有对应的详细记录",
)


def detect_unrecorded_gap(cleaned_reply: str, *, has_any_context: bool) -> bool:
    """Return True when the assistant reply acknowledges missing records or no context exists."""
    return (not has_any_context) or any(m in cleaned_reply for m in UNRECORDED_GAP_MARKERS)


def derive_stream_supplements(
    chat_input: CompanionChatInput,
    *,
    cleaned_reply: str,
    verified_record_ids: Sequence[UUID],
    safety_category: SafetyCategory = SafetyCategory.NORMAL,
) -> CompanionChatOutput:
    """Build a complete `CompanionChatOutput` after streaming `reply_text` chunks.

    BC3 BUG-CC-8a: Never fabricate canned winter-sweater or corporate-email fallback
    strings when the LLM intentionally omits `<whisper>` or `<suggestions>`.
    """
    has_any_context = bool(chat_input.confirmed_facts or chat_input.referenced_materials)
    has_gap = detect_unrecorded_gap(cleaned_reply, has_any_context=has_any_context)

    base = CompanionChatOutput(
        safety_flag=safety_category,
        reply_text=cleaned_reply,
        whisper_note=None,
        cited_record_ids=list(verified_record_ids),
        has_unrecorded_gap=has_gap,
        alternative_perspectives=[],
        actionable_suggestions=[],
    )
    return apply_tier_policy(
        base,
        tier=chat_input.tier,
        user_message=chat_input.user_message,
        safety_category=safety_category,
    )


# ============================================================================
# Continuous Session Window & Late-Night Rest Care (Crisis SOP §3.3)
# ============================================================================


@dataclass(frozen=True, slots=True)
class SessionWindowDecision:
    session_started_at: datetime
    last_user_message_at: datetime
    continuous_minutes: float
    trigger_late_night_care: bool
    care_whisper: str | None


def evaluate_session_window(
    *,
    previous_session_started_at: datetime | None,
    previous_last_message_at: datetime | None,
    now_utc: datetime,
    local_hour: int,
    tier: Tier,
    late_night_care_enabled: bool = True,
    late_night_start_hour: int = 0,
    late_night_end_hour: int = 5,
    late_night_continuous_minutes: int = 45,
    session_reset_gap_minutes: int = 30,
) -> SessionWindowDecision:
    """Update continuous chat session window (resets on >30 min inactivity) & check late-night SOP."""
    reset_gap = timedelta(minutes=session_reset_gap_minutes)
    if (
        previous_session_started_at is None
        or previous_last_message_at is None
        or (now_utc - previous_last_message_at) > reset_gap
    ):
        started_at = now_utc
    else:
        started_at = previous_session_started_at

    continuous_mins = max(0.0, (now_utc - started_at).total_seconds() / 60.0)
    in_late_night = late_night_start_hour <= local_hour < late_night_end_hour
    should_care = (
        late_night_care_enabled
        and in_late_night
        and continuous_mins >= float(late_night_continuous_minutes)
    )
    whisper: str | None = None
    if should_care:
        whisper = (
            LATE_NIGHT_REST_WHISPER_SILVER
            if tier is Tier.SILVER
            else LATE_NIGHT_REST_WHISPER_DEFAULT
        )

    return SessionWindowDecision(
        session_started_at=started_at,
        last_user_message_at=now_utc,
        continuous_minutes=continuous_mins,
        trigger_late_night_care=should_care,
        care_whisper=whisper,
    )


def append_late_night_whisper(existing_whisper: str | None, care_whisper: str | None) -> str | None:
    if not care_whisper:
        return existing_whisper
    if not existing_whisper:
        return care_whisper
    if care_whisper in existing_whisper or "先好好睡一觉" in existing_whisper:
        return existing_whisper
    return f"{existing_whisper} · {care_whisper}"


# ============================================================================
# Token-Budget Trimming for Confirmed Facts (Master Plan §3.3, BC3 BUG-CC-7)
# ============================================================================

PRIORITY_FACT_TYPES: frozenset[FactType] = frozenset(
    {FactType.TABOO, FactType.DATE, FactType.PROMISE}
)


@dataclass(frozen=True, slots=True)
class BudgetCandidateFact:
    fact_id: UUID
    record_id: UUID
    fact_type: FactType
    content: str
    happened_on: str | None = None
    is_expired_one_off: bool = False
    is_unlinked: bool = False


def estimate_fact_tokens(fact: BudgetCandidateFact) -> int:
    # Conservative mixed CJK/ASCII token estimate (~1.5 chars/token => len * 2 // 3 + metadata overhead)
    char_len = len(fact.content) + (len(fact.happened_on) if fact.happened_on else 0)
    return max(1, (char_len * 2 + 2) // 3) + 10


def trim_facts_to_token_budget(
    facts_newest_first: Sequence[BudgetCandidateFact],
    *,
    max_tokens: int = 4000,
    max_chars: int = 6000,
    priority_token_cap: int | None = None,
    priority_char_cap: int | None = None,
) -> list[BudgetCandidateFact]:
    """Include active `taboo`/`date`/`promise` facts up to a guarded cap so preferences never starve.

    BC3 BUG-CC-7:
    - Expired one-off past `DATE` facts (`is_expired_one_off=True`) are demoted below active
      preferences/wishes so years of old appointment dates never crowd out timeless facts.
    - Active priority facts (`TABOO`, active `DATE`, `PROMISE`) are capped at `priority_token_cap`
      (default 55% of `max_tokens`, e.g. 2200/4000 or 1200/2500) so `PREFERENCE`/`WISH` facts
      ALWAYS receive guaranteed prompt budget even on 3-year heavy journals.
    """
    active_priority = [
        f
        for f in facts_newest_first
        if f.fact_type in PRIORITY_FACT_TYPES and not f.is_expired_one_off
    ]
    active_secondary = [
        f
        for f in facts_newest_first
        if f.fact_type not in PRIORITY_FACT_TYPES and not f.is_expired_one_off
    ]
    expired_secondary = [f for f in facts_newest_first if f.is_expired_one_off]

    eff_pri_tokens = (
        priority_token_cap
        if priority_token_cap is not None
        else max(256, int(max_tokens * 0.55))
    )
    eff_pri_chars = (
        priority_char_cap
        if priority_char_cap is not None
        else max(400, int(max_chars * 0.55))
    )

    selected: list[BudgetCandidateFact] = []
    overflow_priority: list[BudgetCandidateFact] = []
    used_tokens = 0
    used_chars = 0

    for fact in active_priority:
        ftok = estimate_fact_tokens(fact)
        fchars = len(fact.content)
        if (
            selected
            and (used_tokens + ftok > eff_pri_tokens or used_chars + fchars > eff_pri_chars)
        ):
            overflow_priority.append(fact)
            continue
        selected.append(fact)
        used_tokens += ftok
        used_chars += fchars

    for fact in (*active_secondary, *overflow_priority, *expired_secondary):
        ftok = estimate_fact_tokens(fact)
        fchars = len(fact.content)
        if used_tokens + ftok > max_tokens or used_chars + fchars > max_chars:
            continue
        selected.append(fact)
        used_tokens += ftok
        used_chars += fchars

    return selected


# ============================================================================
# Declarative Chat Fact Write-Back Eligibility & Deterministic GatePolicy
# ============================================================================

_CLAUSE_SPLIT_RE = re.compile(r"[，,。；;！!？?\n]+")

_DECLARATIVE_FACT_CUE_RE = re.compile(
    r"(?:喜欢|爱吃|爱喝|偏爱|最爱|讨厌|不吃|不能吃|忌口|过敏|雷区|生日|纪念日|"
    r"答应|约定|约好|承诺|打算|想要|想买|想去|想看|心愿|借了|借给|还给|还了|送了|"
    r"改到|定在|每周|下周|周末|下个月|昨天|今天|刚才|刚跟|上次|提到|跟我说|"
    r"记一下|顺便记|帮我记进手帐|记进手帐|备忘|甩锅|推活|抢功|迟到|放鸽子|"
    r"发邮件|加需求|改需求)"
)

_EXPLICIT_RECORDING_CUE_RE = re.compile(r"(?:记一下|顺便记|帮我记进手帐|记进手帐)")

_ASSISTANT_REQUEST_RE = re.compile(
    r"(?:帮我|替我|请帮|给我|列一份|准备清单|设个?提醒|设一个提醒|提前提醒|提醒我|"
    r"整理|梳理|理清|理理|挑份|起草|写一封|写一段|起草一段|生成一段|"
    r"哪些|有什么|留意|注意|守好边界|结合.{0,12}手帐|查一下|翻一下|看看|"
    r"核对.+时间线)"
)

_QUESTION_OR_DRAFT_CMD_RE = re.compile(
    r"(?:什么|怎么|如何|哪天|哪个|哪里|在哪|多少|几号|为什么|是不是|有没有|能不能|"
    r"可不可以|好不好|吗|呢|帮我|替我|请帮|给我|列一份|准备清单|设个?提醒|设一个提醒|"
    r"提前提醒|提醒我|整理|梳理|理清|理理|挑份|起草|写一封|写一段|起草一段|生成一段|"
    r"哪些|有什么|留意|注意|守好边界|结合.{0,12}手帐|查一下|翻一下|看看|"
    r"核对.+时间线)"
)

_EXPLICIT_NOTE_REPORT_RE = re.compile(
    r"(?:记一下|顺便记|帮我记进手帐|记进手帐|备忘|刚跟我说|昨天跟我说|今天跟我说|上次提到)"
)

_TRIVIAL_ACK_RE = re.compile(
    r"^(?:好[的呀啦]?|嗯+|哦+|谢谢[你啦]?|明白[了啦]?|收到|哈哈+|ok|okay|行[的吧]?)$",
    re.IGNORECASE,
)


def should_writeback_chat_message(
    user_message: str,
    safety_category: SafetyCategory = SafetyCategory.NORMAL,
) -> bool:
    """Return True if `user_message` contains declarative personal/relationship statements to extract."""
    if safety_category in (
        SafetyCategory.CRISIS,
        SafetyCategory.VIOLENCE_VICTIM,
        SafetyCategory.ILLEGAL_INTENT,
    ):
        return False

    stripped = user_message.strip()
    if len(stripped) < 4:
        return False
    if _TRIVIAL_ACK_RE.match(stripped):
        return False
    if is_public_shaming_request(stripped):
        return False

    has_explicit_recording_cue = bool(_EXPLICIT_RECORDING_CUE_RE.search(stripped))
    if _ASSISTANT_REQUEST_RE.search(stripped) and not has_explicit_recording_cue:
        return False

    # Split preserving sentence-ending question marks on clauses
    raw_clauses = [
        c.strip() for c in re.split(r"(?<=[。！？!?；;\n])|[，,]", stripped) if c.strip()
    ]
    for clause in raw_clauses:
        if _EXPLICIT_NOTE_REPORT_RE.search(clause) and not clause.endswith(("?", "？")):
            return True
        is_q_or_cmd = clause.endswith(("?", "？")) or bool(_QUESTION_OR_DRAFT_CMD_RE.search(clause))
        if is_q_or_cmd:
            continue
        if _DECLARATIVE_FACT_CUE_RE.search(clause):
            return True

    return False


def decide_chat_writeback_gate(
    *,
    fact_type: FactType,
    confidence: float,
    candidate_count: int,
    multi_person: bool,
    scoped_locked: bool,
    is_venting_or_irony: bool,
    tier: Tier,
    person_state: PersonState,
    source_speaker: SourceSpeaker,
    polarity_flipped: bool = False,
    extraction_ok: bool = True,
    confidence_threshold: float = 0.80,
) -> tuple[FactStatus, PendingReason | None]:
    """Deterministic 8-rule GatePolicy (Invariant I3, BACKEND_MASTER_PLAN §3.1)."""
    if not extraction_ok:
        return FactStatus.PENDING, PendingReason.EXTRACTION_FAILED
    if person_state is PersonState.PROVISIONAL:
        return FactStatus.PENDING, PendingReason.NEW_PERSON
    if candidate_count != 1:
        return FactStatus.PENDING, PendingReason.AMBIGUOUS_PERSON
    if multi_person and not scoped_locked:
        return FactStatus.PENDING, PendingReason.MULTI_PERSON
    # Deliberately NOT the D1 capture semantics (where dates confirm in pencil with an active
    # reminder): chat write-back is an inferred side effect of a conversation, the user never
    # "said this to be recorded", and there is no visible slip/undo receipt in the chat UI yet
    # (roadmap P1 1.4). Confirming here would silently schedule an ACTIVE reminder from a
    # possibly misread date, so chat dates stay pending (they still render as a pencil
    # `date_glance` draft on the person page via the legacy reason mapping).
    if fact_type in (FactType.DATE, FactType.PROMISE):
        return FactStatus.PENDING, PendingReason.DATE_OR_PROMISE
    if is_venting_or_irony and tier is Tier.GOLD:
        return FactStatus.PENDING, PendingReason.VENTING
    if source_speaker is SourceSpeaker.UNKNOWN:
        return FactStatus.PENDING, PendingReason.SPEAKER_UNKNOWN
    if polarity_flipped:
        return FactStatus.PENDING, PendingReason.POLARITY_FLIP
    if confidence < confidence_threshold:
        return FactStatus.PENDING, PendingReason.LOW_CONFIDENCE
    return FactStatus.CONFIRMED, None
