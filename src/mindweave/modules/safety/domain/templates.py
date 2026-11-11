"""Audited fixed crisis intervention card templates from SOP v0.2 §2.2 (Invariant I5).

AI models are strictly forbidden from rewriting these templates. Pure Python domain module.
"""

from __future__ import annotations

from dataclasses import dataclass

from mindweave.shared.enums import SafetyCategory


@dataclass(frozen=True, slots=True)
class HotlineInfo:
    """Verified public helpline entry."""

    name: str
    number: str
    description: str
    sms_number: str | None = None

    def to_dict(self) -> dict[str, str]:
        data: dict[str, str] = {
            "name": self.name,
            "number": self.number,
            "description": self.description,
        }
        if self.sms_number is not None:
            data["sms_number"] = self.sms_number
        return data


@dataclass(frozen=True, slots=True)
class AuditedCrisisTemplate:
    """Immutable audited crisis template matching SOP v0.2 §2.2."""

    template_id: str
    title: str
    care_message: str
    hotlines: tuple[HotlineInfo, ...]
    disclaimer: str

    def hotlines_as_dicts(self) -> list[dict[str, str]]:
        return [h.to_dict() for h in self.hotlines]


CRISIS_SELF_HARM_V1 = AuditedCrisisTemplate(
    template_id="crisis_self_harm_v1",
    title="心络温暖守护提示：此刻请先照顾好你自己",
    care_message=(
        "看到你写下的这些话，我们非常担心你现在的状态。"
        "你正在经历的痛苦和委屈是真实的，但请一定不要一个人扛着。"
        "请立刻停下来，联系以下 24 小时免费专业支持渠道，或者给你身边信任的家人、朋友打个电话："
    ),
    hotlines=(
        HotlineInfo(
            name="全国统一心理援助热线",
            number="12356",
            description="卫健委设立，24小时心理疏导与危机干预",
        ),
        HotlineInfo(
            name="希望24热线",
            number="400-161-9995",
            description="全国24小时免费心理危机干预",
        ),
        HotlineInfo(
            name="紧急医疗救援电话",
            number="120",
            description="24小时紧急医疗救助",
        ),
        HotlineInfo(
            name="紧急报警求助电话",
            number="110",
            description="24小时公安紧急报警求助",
        ),
    ),
    disclaimer="作为一款私密手账工具，AI 无法替代专业人士和现实中在乎你的人给你真正的支持。",
)

DOMESTIC_VIOLENCE_V1 = AuditedCrisisTemplate(
    template_id="domestic_violence_v1",
    title="安全求助提示：你的人身安全高于一切",
    care_message=(
        "如果你正在遭受暴力对待、威胁或人身限制，"
        "请优先确保自己处于安全的环境中，并及时向警方或专业机构求助："
    ),
    hotlines=(
        HotlineInfo(
            name="紧急报警电话",
            number="110",
            sms_number="12110",
            description="不便语音时可发送位置短信至 12110 短信报警",
        ),
        HotlineInfo(
            name="全国妇联妇女维权公益服务热线",
            number="12338",
            description="妇女维权与反家暴公益服务热线",
        ),
        HotlineInfo(
            name="全国统一心理援助热线",
            number="12356",
            description="卫健委设立，24小时心理疏导与危机干预",
        ),
    ),
    disclaimer="请优先确保自身处于安全环境；不便语音通话时可发送位置短信至 12110 报警求助。",
)

AUDITED_TEMPLATES_BY_ID: dict[str, AuditedCrisisTemplate] = {
    CRISIS_SELF_HARM_V1.template_id: CRISIS_SELF_HARM_V1,
    DOMESTIC_VIOLENCE_V1.template_id: DOMESTIC_VIOLENCE_V1,
}


def get_crisis_template_for_category(category: SafetyCategory) -> AuditedCrisisTemplate | None:
    """Return the audited fixed template for a crisis category, or None if non-crisis."""
    if category is SafetyCategory.CRISIS:
        return CRISIS_SELF_HARM_V1
    if category is SafetyCategory.VIOLENCE_VICTIM:
        return DOMESTIC_VIOLENCE_V1
    return None
