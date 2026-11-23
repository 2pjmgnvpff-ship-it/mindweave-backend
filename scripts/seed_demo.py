"""One-command Demo Day Showcase Seeder for MindWeave (心络).

Usage:
    uv run python scripts/seed_demo.py --email demo@mindweave.app
    uv run python scripts/seed_demo.py --email demo@mindweave.app --reset

Provisions (or reuses) a real PostgreSQL user with FORCE RLS, per-user AES-256-GCM DEK
envelope encryption, authentic cross-month journal slips, live D1 pencil drafts,
structured confirmed facts, active solar/lunar reminders, and a real encrypted WAV voice
memo for browser playback.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import math
import struct
import wave
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.core.config import get_settings
from mindweave.core.dependencies import AppContainer
from mindweave.core.dto import FORBIDDEN_UI_DEV_PHRASES
from mindweave.core.ids import new_id
from mindweave.core.orm import FactRow, PersonRow, RecordRow
from mindweave.core.security import hash_contact_lookup
from mindweave.modules.identity.domain.models import AuthProvider, normalize_email
from mindweave.modules.identity.infra.repositories import (
    IdentitySystemRepository,
    IdentityUserRepository,
)
from mindweave.modules.people.domain.entities import default_visual_tokens
from mindweave.modules.people.domain.tier_transition import compute_new_person_rank
from mindweave.modules.people.infra.repository import PeopleRepository
from mindweave.shared.enums import (
    AliasKind,
    ConsentKind,
    CreatedBy,
    FactStatus,
    FactType,
    PersonState,
    Polarity,
    RecordKind,
    RecordReviewState,
    ReminderStatus,
    SourceSpeaker,
    Tier,
    TimeStatus,
)
from mindweave.shared.scopes import UserScope

FORBIDDEN_PROTOTYPE_NAMES: tuple[str, ...] = ("沈清秋", "周叙白", "林知夏")
DEMO_SLUG_PREFIX = "demo-"
DEFAULT_DEMO_EMAIL = "demo@mindweave.app"


@dataclass(frozen=True, slots=True)
class DemoFactSpec:
    fact_type: FactType
    content: str
    polarity: Polarity = Polarity.NEUTRAL
    trigger_date: date | None = None
    is_lunar: bool = False
    lunar_month: int | None = None
    lunar_day: int | None = None
    lunar_is_leap_month: bool = False
    needs_glance: bool = False
    create_reminder: bool = False


@dataclass(frozen=True, slots=True)
class DemoSlipSpec:
    happened_on: date
    title: str
    body: str
    kind: RecordKind = RecordKind.NOTE
    pinned: bool = False
    stamp_code: str | None = None
    has_demo_audio: bool = False
    audio_duration_ms: int | None = None
    facts: tuple[DemoFactSpec, ...] = ()


@dataclass(frozen=True, slots=True)
class DemoPersonaSpec:
    slug: str
    name_cn: str
    monogram: str
    tier: Tier
    role_tag: str
    context_sub: str
    short_memo: str
    traits: tuple[str, ...]
    aliases: tuple[tuple[str, AliasKind], ...] = ()
    slips: tuple[DemoSlipSpec, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class DemoSeedSummary:
    email: str
    user_id: UUID
    created_new_user: bool
    reused_existing_demo_data: bool
    person_count: int
    slip_count: int
    fact_count: int
    pencil_draft_count: int
    reminder_count: int
    voice_blob_count: int


def build_demo_wav_bytes(*, duration_sec: float = 3.2, sample_rate: int = 8000) -> bytes:
    """Synthesize a valid, soft, browser-playable 16-bit mono PCM WAV audio blob."""
    total_frames = max(1, int(duration_sec * sample_rate))
    buf = io.BytesIO()
    # Gentle warm C-major triad chime (C4/E4/G4) with smooth attack/decay envelope
    freqs = (261.63, 329.63, 392.00)
    samples = bytearray()

    for idx in range(total_frames):
        t = idx / sample_rate
        # Soft fade-in over 0.25s and warm exponential decay over the rest of the clip
        attack = min(1.0, t / 0.25)
        decay = math.exp(-t * 1.15)
        fade_out = min(1.0, max(0.0, (duration_sec - t) / 0.25))
        env = attack * decay * fade_out
        wave_val = sum(math.sin(2.0 * math.pi * f * t) for f in freqs) / len(freqs)
        pcm = int(max(-32767, min(32767, round(wave_val * env * 2200))))
        samples.extend(struct.pack("<h", pcm))

    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(samples))
    return buf.getvalue()


def _make_snippet(body: str, max_len: int = 76) -> str:
    compact = " ".join(body.split()).strip()
    if len(compact) <= max_len:
        return compact
    return compact[: max_len - 1].rstrip() + "…"


def build_demo_personas() -> tuple[DemoPersonaSpec, ...]:
    """Return the curated Demo Day personas with zero forbidden prototype names or jargon."""
    personas: tuple[DemoPersonaSpec, ...] = (
        # 1. Gold Tier — 妈妈 (16 rich cross-month slips spanning 2025-12 to 2026-11)
        DemoPersonaSpec(
            slug="demo-mom",
            name_cn="妈妈",
            monogram="妈",
            tier=Tier.GOLD,
            role_tag="最牵挂的家人",
            context_sub="总说不用惦记，其实每件小事都藏着念想",
            short_memo="右膝盖阴雨天容易酸痛，香菜过敏，爱喝桂花乌龙，最拿手糖醋排骨。",
            traits=("香菜过敏", "爱喝桂花乌龙", "右膝盖11月28日骨科复查", "农历五月十二生日"),
            aliases=(("老妈", AliasKind.NICKNAME), ("妈", AliasKind.TITLE)),
            slips=(
                DemoSlipSpec(
                    happened_on=date(2025, 12, 21),
                    title="冬至橱窗前的浅驼色羊绒围巾",
                    body=(
                        "去年冬至傍晚陪妈妈逛街，路过橱窗她摸着浅驼色羊绒围巾说软软的真暖和，"
                        "翻看吊牌后又连忙摆手说家里围巾够戴了。今年降温前一定要悄悄买来送她。"
                    ),
                    pinned=True,
                    stamp_code="heart_wish",
                ),
                DemoSlipSpec(
                    happened_on=date(2026, 1, 14),
                    title="妈妈教的糖醋排骨比例",
                    body=(
                        "腊月在厨房跟妈妈学糖醋排骨，她念叨着最稳的比例：1勺料酒、2勺生抽、"
                        "3勺白糖、4勺陈醋、5勺清水，大火烧开转小火慢慢收汁到发亮，出锅前撒点白芝麻，"
                        "千万别放香菜。"
                    ),
                    pinned=True,
                    stamp_code="kitchen_recipe",
                ),
                DemoSlipSpec(
                    happened_on=date(2026, 2, 2),
                    title="年夜饭备菜牢记避开香菜",
                    body=(
                        "过年准备炖汤和凉拌菜时妈妈又提醒了一次，她对香菜过敏，"
                        "吃到一点就容易喉咙发痒起红疹，外出聚餐点菜必须提前叮嘱服务员免香菜。"
                    ),
                    facts=(
                        DemoFactSpec(
                            fact_type=FactType.TABOO,
                            polarity=Polarity.NEGATIVE,
                            content="对香菜过敏，做菜和外出点餐务必完全避开香菜",
                        ),
                    ),
                ),
                DemoSlipSpec(
                    happened_on=date(2026, 2, 20),
                    title="午后晒太阳泡桂花乌龙茶",
                    body=(
                        "回老家午后陪妈妈喝茶，她新拆了一罐桂花乌龙，说这茶清香温润不刮胃，"
                        "比浓绿茶舒服得多，配两块低糖绿豆糕刚好。"
                    ),
                    facts=(
                        DemoFactSpec(
                            fact_type=FactType.PREFERENCE,
                            polarity=Polarity.POSITIVE,
                            content="偏爱清香不刮胃的桂花乌龙茶，喜欢配低糖中式茶点",
                        ),
                    ),
                ),
                DemoSlipSpec(
                    happened_on=date(2026, 3, 11),
                    title="倒春寒右膝盖有些发凉",
                    body=(
                        "三月中旬降温下雨，妈妈在电话里说上下楼梯时右膝盖有点发紧发凉，"
                        "自己把旧护膝找出来戴上了，嘱咐我买家居裤尽量选裤腿宽松、不勒膝盖的纯棉款。"
                    ),
                ),
                DemoSlipSpec(
                    happened_on=date(2026, 4, 6),
                    title="南阳台那盆茉莉抽了新芽",
                    body=(
                        "清明回家看见妈妈把南阳台的茉莉换了个透气陶盆，她习惯清早浇水修剪枯枝，"
                        "说一推开纱窗闻到茉莉花香，一整天心情都很轻快。"
                    ),
                ),
                DemoSlipSpec(
                    happened_on=date(2026, 5, 3),
                    title="陪妈妈试穿软底防滑健步鞋",
                    body=(
                        "周末陪妈妈挑了双软底防滑健步鞋，她脚背偏高，穿37码半最合脚不挤脚背，"
                        "鞋底一定要纹路深、防滑耐磨，雨天去菜场走路才踏实。"
                    ),
                    facts=(
                        DemoFactSpec(
                            fact_type=FactType.PREFERENCE,
                            polarity=Polarity.POSITIVE,
                            content="穿37码半宽松防滑软底鞋，脚背偏高偏爱轻便不挤脚鞋型",
                        ),
                    ),
                ),
                DemoSlipSpec(
                    happened_on=date(2026, 6, 15),
                    title="梅雨季煮不加糖的红豆薏米水",
                    body=(
                        "入梅后空气潮，妈妈每天上午煮一壶无糖红豆薏米水装进保温杯，"
                        "边喝边提醒我也少喝太冰的冷饮。"
                    ),
                ),
                DemoSlipSpec(
                    happened_on=date(2026, 7, 19),
                    title="三伏天午睡怕空调直吹膝盖",
                    body=(
                        "夏天回家发现妈妈总把空调定在27度还要盖一条薄棉毯，说右膝盖受不了冷风直吹，"
                        "下次顺手给她装个可调节的空调挡风板。"
                    ),
                ),
                DemoSlipSpec(
                    happened_on=date(2026, 8, 22),
                    title="早市捎回来的手剥鲜莲子",
                    body=(
                        "妈妈一早绕路去老菜场买了两斤带蓬鲜莲子，坐客厅一颗颗挑完莲心分装冷冻，"
                        "让我返程带上煮银耳莲子羹。"
                    ),
                ),
                DemoSlipSpec(
                    happened_on=date(2026, 9, 18),
                    title="中秋提前寄回低糖苏式月饼",
                    body=(
                        "今年选了低糖黑芝麻和桂花馅的苏式月饼寄回家，妈妈回语音说外皮酥、甜度克制，"
                        "晚饭后切半块配桂花乌龙茶特别合适。"
                    ),
                ),
                DemoSlipSpec(
                    happened_on=date(2026, 10, 5),
                    title="国庆公园赏桂走四十来分钟歇脚",
                    body=(
                        "国庆陪妈妈去植物园散步看金桂，她走满四十分钟就需要找长椅坐下歇会儿右腿，"
                        "一路兴致很高拍了好几张桂花枝发家族群。"
                    ),
                ),
                DemoSlipSpec(
                    happened_on=date(2026, 10, 24),
                    title="霜降寄去的羊绒护膝很贴合",
                    body=(
                        "晚风转凉前寄了两副加厚羊绒护膝和暖贴回家，妈妈收到当晚就试戴了一圈，"
                        "说比以前那副轻软贴合，走路不会往下滑。"
                    ),
                ),
                DemoSlipSpec(
                    happened_on=date(2026, 11, 8),
                    title="妈妈电话 · 桂花乌龙快喝完了",
                    body=("家里桂花乌龙快见底了，降温了你早晚记得添外套，工作再忙也要按时吃晚饭。"),
                    kind=RecordKind.VOICE,
                    has_demo_audio=True,
                    audio_duration_ms=3200,
                ),
                DemoSlipSpec(
                    happened_on=date(2026, 11, 11),
                    title="对香菜过敏要提醒免香菜",
                    body="对香菜过敏，聚餐点菜记得免香菜。",
                ),
                DemoSlipSpec(
                    happened_on=date(2026, 11, 13),
                    title="脚背偏高穿37码半软底鞋",
                    body="穿37码半软底防滑鞋，脚背偏高不挤脚。",
                ),
                DemoSlipSpec(
                    happened_on=date(2026, 11, 16),
                    title="右膝盖疼痛 · 约好11月28日骨科复查",
                    body=(
                        "阴雨天妈妈右膝盖上下楼梯还是隐隐作痛，已挂好11月28日上午市一院骨科复查号，"
                        "记得带去年的核磁胶片和医保卡，我请半天假陪她去。"
                    ),
                    pinned=True,
                    stamp_code="care_checkup",
                    facts=(
                        DemoFactSpec(
                            fact_type=FactType.DATE,
                            polarity=Polarity.NEUTRAL,
                            content="陪妈妈去市一院做右膝盖骨科复查（带齐去年核磁片和医保卡）",
                            trigger_date=date(2026, 11, 28),
                            needs_glance=False,
                            create_reminder=True,
                        ),
                    ),
                ),
                DemoSlipSpec(
                    happened_on=date(2026, 11, 21),
                    title="妈妈农历五月十二生日",
                    body="妈妈一直过农历五月十二生日，明年提前订低糖蛋糕，别放香菜装饰。",
                    facts=(
                        DemoFactSpec(
                            fact_type=FactType.DATE,
                            polarity=Polarity.POSITIVE,
                            content="妈妈农历五月十二生日（提前一周准备贴心礼物）",
                            is_lunar=True,
                            lunar_month=5,
                            lunar_day=12,
                            needs_glance=True,
                            create_reminder=True,
                        ),
                    ),
                ),
            ),
        ),
        # 2. Gold Tier — 阿远 (6 warm slips across 5 months)
        DemoPersonaSpec(
            slug="demo-ayuan",
            name_cn="阿远",
            monogram="远",
            tier=Tier.GOLD,
            role_tag="多年挚友",
            context_sub="背胶片机吹海风的老搭档，靠谱又嘴硬心软",
            short_memo="对花生严重过敏，最爱手冲水洗耶加雪菲，约好12月6日冬日海边胶片行。",
            traits=("花生过敏", "爱喝水洗耶加雪菲", "胶片摄影同好", "3月14日生日"),
            aliases=(("远哥", AliasKind.NICKNAME),),
            slips=(
                DemoSlipSpec(
                    happened_on=date(2026, 7, 12),
                    title="聚餐务必避开花生与花生碎",
                    body=(
                        "晚上一起吃川菜差点点了宫保鸡丁，阿远笑着提醒他对花生严重过敏，"
                        "连蘸料和甜品表面撒的花生碎都不能碰，以后聚餐挑菜先问清楚配料。"
                    ),
                    facts=(
                        DemoFactSpec(
                            fact_type=FactType.TABOO,
                            polarity=Polarity.NEGATIVE,
                            content="对花生严重过敏，聚餐点菜和甜品务必避开花生及花生碎",
                        ),
                    ),
                ),
                DemoSlipSpec(
                    happened_on=date(2026, 8, 19),
                    title="手冲偏爱水洗耶加雪菲",
                    body=(
                        "周末钻进老城巷子里的手冲咖啡馆，阿远点了一杯热手冲水洗耶加雪菲，"
                        "说最喜欢那种干净明亮的柑橘和白花香气，平时很少碰加糖奶咖。"
                    ),
                    facts=(
                        DemoFactSpec(
                            fact_type=FactType.PREFERENCE,
                            polarity=Polarity.POSITIVE,
                            content="最爱热手冲水洗耶加雪菲，偏爱柑橘白花风味、不加糖奶",
                        ),
                    ),
                ),
                DemoSlipSpec(
                    happened_on=date(2026, 9, 26),
                    title="借给我备用胶片机身还手写测光笔记",
                    body=(
                        "阿远周末专程把保养好的老胶片机身借我试拍，还在相机包夹层里塞了半页手写"
                        "测光和快门速度对照，嘴上嫌麻烦其实特别细心。"
                    ),
                ),
                DemoSlipSpec(
                    happened_on=date(2026, 10, 29),
                    title="深夜聊起冬日淡季海边拍胶片",
                    body=(
                        "秋夜吃完宵夜聊到想找个周末去冬日空旷的海岸线拍两卷胶片，"
                        "趁游客少，慢慢等日落时分的蓝调海面。"
                    ),
                ),
                DemoSlipSpec(
                    happened_on=date(2026, 11, 14),
                    title="12月6日冬日海边胶片行",
                    body=(
                        "和阿远正式敲定12月6日去东海岸拍冬日海边胶片，早七点半高铁站进站口集合，"
                        "我带保温壶装刚冲好的水洗耶加雪菲和两卷柯达金200，提醒他穿防风外套。"
                    ),
                    pinned=True,
                    stamp_code="film_trip",
                    facts=(
                        DemoFactSpec(
                            fact_type=FactType.PROMISE,
                            polarity=Polarity.POSITIVE,
                            content="12月6日和阿远赴冬日海边胶片行（早7:30高铁站集合）",
                            trigger_date=date(2026, 12, 6),
                            needs_glance=False,
                            create_reminder=True,
                        ),
                    ),
                ),
                DemoSlipSpec(
                    happened_on=date(2026, 11, 20),
                    title="阿远3月14日生日",
                    body=(
                        "顺手记下阿远生日是3月14日，明年提前挑一卷罕见彩色负片或手工陶瓷滤杯当礼物，"
                        "切记避开含花生的坚果伴手礼。"
                    ),
                    facts=(
                        DemoFactSpec(
                            fact_type=FactType.DATE,
                            polarity=Polarity.POSITIVE,
                            content="阿远3月14日生日（提前准备胶片或手冲咖啡礼物）",
                            trigger_date=date(2027, 3, 14),
                            needs_glance=True,
                            create_reminder=True,
                        ),
                    ),
                ),
            ),
        ),
        # 3. Silver Tier — 陈经理 (6 strictly factual workplace-boundary slips, ZERO photos!)
        DemoPersonaSpec(
            slug="demo-mgr-chen",
            name_cn="陈经理",
            monogram="陈",
            tier=Tier.SILVER,
            role_tag="直属业务主管",
            context_sub="口头承诺易反悔，关键节点一律邮件留痕",
            short_memo="所有需求变更与排期确认均需书面邮件时间戳留档，守住工作边界。",
            traits=("口头拍板易反悔", "复盘会爱甩锅", "周五傍晚临时加需求", "必须邮件抄送留痕"),
            aliases=(("老陈", AliasKind.NICKNAME), ("陈总", AliasKind.TITLE)),
            slips=(
                DemoSlipSpec(
                    happened_on=date(2026, 8, 14),
                    title="8月14日口头拍板砍项后次周反悔",
                    body=(
                        "2026年8月14日下午周会，陈经理口头确认二期看板暂不做CSV导出模块；"
                        "8月19日却在项目大群质问为何漏做导出功能。此后所有口径调整一律当场补发纪要邮件。"
                    ),
                    facts=(
                        DemoFactSpec(
                            fact_type=FactType.EVENT,
                            polarity=Polarity.NEGATIVE,
                            content="2026-08-14口头确认暂不做导出模块，8月19日反悔未认账；此后坚持书面纪要留痕",
                        ),
                    ),
                ),
                DemoSlipSpec(
                    happened_on=date(2026, 9, 4),
                    title="9月4日跨组接口延期客观边界留档",
                    body=(
                        "2026年9月4日14:10已通过项目群与邮件同步上游数据接口延期3个工作日的客观事实，"
                        "并抄送测试负责人，明确联调节点顺延并非执行侧拖期。"
                    ),
                ),
                DemoSlipSpec(
                    happened_on=date(2026, 9, 25),
                    title="9月25日临时借调书面确认优先级置换",
                    body=(
                        "9月25日上午陈经理要求临时支援大客户汇报材料。已于10:35邮件列出当前主线排期与"
                        "预计耗时，请其书面回复确认被置换项，避免后续追责主线进度。"
                    ),
                ),
                DemoSlipSpec(
                    happened_on=date(2026, 10, 9),
                    title="10月9日16:22需求冻结签字确认邮件",
                    body=(
                        "2026年10月9日16:22收到陈经理回复《Q3交付范围冻结确认》邮件，正文明确签字确认"
                        "“交付清单与10月15日上线窗口已锁定，无新增验收项”，原邮件及抄送人记录已完整留存。"
                    ),
                    facts=(
                        DemoFactSpec(
                            fact_type=FactType.EVENT,
                            polarity=Polarity.NEUTRAL,
                            content="2026-10-09 16:22 陈经理邮件书面签字确认Q3范围冻结及10月15日上线窗口",
                        ),
                    ),
                ),
                DemoSlipSpec(
                    happened_on=date(2026, 10, 18),
                    title="10月18日Q3复盘会当众甩锅事实记录",
                    body=(
                        "2026年10月18日Q3复盘会上，陈经理当着部门总监面称“边缘报表缺失是执行同学自行删减"
                        "需求”，只字未提10月9日16:22其本人邮件签字冻结的范围清单。会后已附原邮件截图和时间线"
                        "抄送总监与PMO作客观澄清。"
                    ),
                    facts=(
                        DemoFactSpec(
                            fact_type=FactType.EVENT,
                            polarity=Polarity.NEGATIVE,
                            content="2026-10-18 Q3复盘会当众甩锅称执行侧擅自删项，与10月9日16:22冻结邮件事实相悖",
                        ),
                    ),
                ),
                DemoSlipSpec(
                    happened_on=date(2026, 11, 13),
                    title="周五17:45临时加需求邮件留痕",
                    body=(
                        "2026年11月13日周五17:45，陈经理邮件临时追加两张非计划内BI经营分析表并要求周一"
                        "早十点前上线。已于17:58回复邮件并抄送项目经理：列明新增需求评估为2.5人日，请书面"
                        "确认顺延主线排期或排入下周迭代，不接受口头承诺。"
                    ),
                    pinned=True,
                    stamp_code="email_trail",
                    facts=(
                        DemoFactSpec(
                            fact_type=FactType.EVENT,
                            polarity=Polarity.NEGATIVE,
                            content="2026-11-13 周五17:45临时追加两张BI报表需求，已于17:58书面邮件评估2.5人日并抄送PMO",
                        ),
                    ),
                ),
            ),
        ),
        # 4–7. Crystal Tier — 记繁琐名册 (4 realistic casual contacts, 2 crisp notes each)
        DemoPersonaSpec(
            slug="demo-dr-lin",
            name_cn="林医生",
            monogram="林",
            tier=Tier.CRYSTAL,
            role_tag="市一院骨科副主任",
            context_sub="周二、周六上午专家门诊，看片极细致",
            short_memo="市一院门诊三楼4诊室，复诊务必带齐一年内原始核磁胶片与旧病历。",
            traits=("周二/周六上午出诊", "习惯先看原始核磁片"),
            aliases=(("林主任", AliasKind.TITLE),),
            slips=(
                DemoSlipSpec(
                    happened_on=date(2026, 9, 12),
                    title="市一院骨科放号时间与复诊清单",
                    body=(
                        "林医生每周二、周六上午在市一院门诊三楼4诊室出诊，提前7天早七点放号。"
                        "复查膝关节一定要带上原始核磁胶片和上次门诊病历本。"
                    ),
                    facts=(
                        DemoFactSpec(
                            fact_type=FactType.NOTE,
                            content="市一院骨科门诊三楼4诊室，周二/周六上午出诊，复查需带原始核磁胶片",
                        ),
                    ),
                ),
                DemoSlipSpec(
                    happened_on=date(2026, 10, 20),
                    title="膝关节日常保暖与热敷叮嘱",
                    body=(
                        "林医生交代中老年膝关节养护尽量少爬陡坡、避免深蹲，秋冬注意护膝保暖，"
                        "睡前温热敷15分钟即可。"
                    ),
                ),
            ),
        ),
        DemoPersonaSpec(
            slug="demo-zhang-shifu",
            name_cn="物业张师傅",
            monogram="张",
            tier=Tier.CRYSTAL,
            role_tag="小区工程维修",
            context_sub="水电门锁都拿手，收费透明利落",
            short_memo="2号岗亭值班，上午九点到十一点约上门最方便。",
            traits=("上午9-11点方便上门", "自带配件收费实在"),
            aliases=(("张师傅", AliasKind.NICKNAME),),
            slips=(
                DemoSlipSpec(
                    happened_on=date(2026, 8, 28),
                    title="更换厨房角阀与阳台移门微调",
                    body=(
                        "张师傅上午十点准时上门换好厨房不锈钢冷热角阀，自带生料带只收配件成本价，"
                        "临走顺手把阳台移门滑轮也调顺了。"
                    ),
                    facts=(
                        DemoFactSpec(
                            fact_type=FactType.NOTE,
                            content="小区工程维修张师傅上午9-11点最方便上门，换水电配件透明靠谱",
                        ),
                    ),
                ),
                DemoSlipSpec(
                    happened_on=date(2026, 10, 11),
                    title="入冬前免费清洗地暖滤网预约",
                    body=(
                        "张师傅提醒每年11月初物业提供免费地暖滤网清洗，提前一天在微信留言约上午时段"
                        "不用排队。"
                    ),
                ),
            ),
        ),
        DemoPersonaSpec(
            slug="demo-designer-zhou",
            name_cn="设计师小周",
            monogram="周",
            tier=Tier.CRYSTAL,
            role_tag="合作视觉设计师",
            context_sub="极简排版克制耐看，反馈效率高",
            short_memo="开案前先发三张参考图和字体偏好，序号化改稿最省沟通成本。",
            traits=("回复利落", "偏好先定版式参考再细调配色"),
            aliases=(("小周", AliasKind.NICKNAME),),
            slips=(
                DemoSlipSpec(
                    happened_on=date(2026, 9, 8),
                    title="最高效的需求对齐方式",
                    body=(
                        "和小周合作展板与画册时，先给3张情绪参考图和明确截稿节点，修改意见按1/2/3序号"
                        "一次发完，返稿又快又准。"
                    ),
                    facts=(
                        DemoFactSpec(
                            fact_type=FactType.PREFERENCE,
                            content="给设计师小周提需求附3张参考图、修改意见按序号整理，配合效率最高",
                        ),
                    ),
                ),
                DemoSlipSpec(
                    happened_on=date(2026, 11, 2),
                    title="结算比例与电子发票流程",
                    body=(
                        "项目合作按50%预付、终稿验收后三个工作日内结清尾款，可开正规设计服务类"
                        "电子普通发票。"
                    ),
                ),
            ),
        ),
        DemoPersonaSpec(
            slug="demo-teahouse-owner",
            name_cn="茶室老板娘",
            monogram="茶",
            tier=Tier.CRYSTAL,
            role_tag="松风茶舍主理人",
            context_sub="老城巷口安静雅座，识茶很准",
            short_memo="每年十月头茬桂花乌龙品质最稳，周末下午二楼窗边包间需提前两小时留位。",
            traits=("十月头茬桂花乌龙", "周末提前两小时留窗边座"),
            aliases=(("松风老板娘", AliasKind.NICKNAME),),
            slips=(
                DemoSlipSpec(
                    happened_on=date(2026, 10, 3),
                    title="头茬手工窨制桂花乌龙茶礼盒",
                    body=(
                        "巷口松风茶舍老板娘每年十月自己窨一批桂花乌龙，茶汤清甜不苦涩，"
                        "按半斤礼盒装带回家给妈妈喝特别合适。"
                    ),
                    facts=(
                        DemoFactSpec(
                            fact_type=FactType.NOTE,
                            content="松风茶舍十月头茬桂花乌龙品质稳定，可订半斤装礼盒",
                        ),
                    ),
                ),
                DemoSlipSpec(
                    happened_on=date(2026, 11, 10),
                    title="二楼窗边包间预订与巷口停车",
                    body=(
                        "茶舍二楼靠窗小包间可坐四人，周末下午人多需提前两小时微信留座，"
                        "斜对面弄堂口可免费停两小时。"
                    ),
                ),
            ),
        ),
    )

    # Hard verification: zero forbidden prototype names & zero forbidden UI dev jargon
    for person in personas:
        texts = [
            person.name_cn,
            person.monogram,
            person.role_tag,
            person.context_sub,
            person.short_memo,
            *person.traits,
            *(alias for alias, _ in person.aliases),
        ]
        for slip in person.slips:
            texts.extend([slip.title, slip.body])
            texts.extend(f.content for f in slip.facts)
        for text_val in texts:
            for forbidden_name in FORBIDDEN_PROTOTYPE_NAMES:
                assert forbidden_name not in text_val, (
                    f"Forbidden prototype name {forbidden_name!r} found in demo persona {person.slug!r}"
                )
            for forbidden_phrase in FORBIDDEN_UI_DEV_PHRASES:
                assert forbidden_phrase not in text_val, (
                    f"Forbidden dev phrase {forbidden_phrase!r} found in demo persona {person.slug!r}"
                )

    return personas


async def _provision_or_load_demo_user(
    container: AppContainer,
    *,
    normalized_email: str,
    tz_name: str,
) -> tuple[UUID, bytes, bool]:
    """Provision a real user + wrapped AES-256-GCM DEK (or load existing) under FORCE RLS."""
    sys_repo = IdentitySystemRepository()
    user_repo = IdentityUserRepository()
    now = container.clock.now()
    email_hmac = hash_contact_lookup(normalized_email, secret=container.settings.jwt_secret)

    existing_user_id: UUID | None = None
    async with container.db.system_session() as sys_sess:
        existing_ident = await sys_repo.get_auth_identity(
            sys_sess,
            provider=AuthProvider.EMAIL,
            subject_hmac=email_hmac,
        )
        if existing_ident is not None:
            existing_user_id = existing_ident.user_id

    if existing_user_id is not None:
        scope = UserScope(existing_user_id)
        async with container.db.user_session(scope) as user_sess:
            dek = await container.load_user_dek(user_sess, scope)
            for kind in (
                ConsentKind.PRIVACY_POLICY,
                ConsentKind.SENSITIVE_PERSONAL_INFO,
                ConsentKind.AI_PROCESSING,
            ):
                await user_repo.record_consent(
                    user_sess,
                    scope=scope,
                    consent_kind=kind,
                    policy_version="2026.11",
                    granted=True,
                    now=now,
                )
        return existing_user_id, dek, False

    user_id = new_id()
    identity_id = new_id()
    wrapped_dek, kek_id = container.cipher.generate_wrapped_dek(user_id)
    dek = container.cipher.load_dek(
        user_id=user_id,
        wrapped_dek=wrapped_dek,
        kek_id=kek_id,
    )
    contact_enc = container.cipher.encrypt_str(
        dek,
        normalized_email,
        user_id=user_id,
        table="auth_identities",
        column="contact_enc",
        row_id=identity_id,
    )

    async with container.db.system_session() as sys_sess:
        await sys_repo.create_user_and_identity(
            sys_sess,
            user_id=user_id,
            identity_id=identity_id,
            provider=AuthProvider.EMAIL,
            subject_hmac=email_hmac,
            contact_enc=contact_enc,
            now=now,
        )

    scope = UserScope(user_id)
    async with container.db.user_session(scope) as user_sess:
        await user_repo.provision_user_workspace(
            user_sess,
            scope=scope,
            wrapped_dek=wrapped_dek,
            kek_id=kek_id,
            default_timezone=tz_name,
            now=now,
        )
        for kind in (
            ConsentKind.PRIVACY_POLICY,
            ConsentKind.SENSITIVE_PERSONAL_INFO,
            ConsentKind.AI_PROCESSING,
        ):
            await user_repo.record_consent(
                user_sess,
                scope=scope,
                consent_kind=kind,
                policy_version="2026.11",
                granted=True,
                now=now,
            )

    return user_id, dek, True


async def _wipe_existing_demo_personas(
    session: AsyncSession,
    container: AppContainer,
    scope: UserScope,
) -> list[str]:
    """Remove previously seeded `demo-*` personas (cascading children) and collect old blob keys."""
    existing_persons = list(
        (
            await session.scalars(
                select(PersonRow).where(
                    PersonRow.user_id == scope.user_id,
                    PersonRow.slug.like(f"{DEMO_SLUG_PREFIX}%"),
                )
            )
        ).all()
    )
    if not existing_persons:
        return []

    person_ids = [p.id for p in existing_persons]
    blob_keys = [
        key
        for key in (
            await session.scalars(
                select(RecordRow.media_object_key).where(
                    RecordRow.user_id == scope.user_id,
                    RecordRow.person_id.in_(person_ids),
                    RecordRow.media_object_key.is_not(None),
                )
            )
        ).all()
        if key
    ]
    for person in existing_persons:
        await session.delete(person)
    await session.flush()
    return blob_keys


async def seed_demo_workspace(
    *,
    email: str = DEFAULT_DEMO_EMAIL,
    reset: bool = False,
    tz_name: str = "Asia/Shanghai",
) -> DemoSeedSummary:
    """Seed the complete Demo Day showcase workspace for `email`."""
    # Ensure `capture` initializes before `memory` to avoid package-init import cycles
    import mindweave.modules.capture  # noqa: F401
    from mindweave.modules.memory.infra.reminders_helper import upsert_reminder_for_fact

    normalized_email = normalize_email(email)
    settings = get_settings()
    container = AppContainer.build(settings)
    personas = build_demo_personas()
    wav_bytes = build_demo_wav_bytes()

    try:
        user_id, dek, created_new_user = await _provision_or_load_demo_user(
            container,
            normalized_email=normalized_email,
            tz_name=tz_name,
        )
        scope = UserScope(user_id)
        people_repo = PeopleRepository(container.cipher)
        now = container.clock.now()
        tz = ZoneInfo(tz_name)
        old_blobs_to_delete: list[str] = []

        async with container.db.user_session(scope) as session:
            existing_demo_count = len(
                (
                    await session.scalars(
                        select(PersonRow.id).where(
                            PersonRow.user_id == scope.user_id,
                            PersonRow.slug.like(f"{DEMO_SLUG_PREFIX}%"),
                            PersonRow.deleted_at.is_(None),
                        )
                    )
                ).all()
            )
            if existing_demo_count > 0 and not reset:
                return DemoSeedSummary(
                    email=normalized_email,
                    user_id=user_id,
                    created_new_user=created_new_user,
                    reused_existing_demo_data=True,
                    person_count=existing_demo_count,
                    slip_count=0,
                    fact_count=0,
                    pencil_draft_count=0,
                    reminder_count=0,
                    voice_blob_count=0,
                )

            if existing_demo_count > 0 and reset:
                old_blobs_to_delete = await _wipe_existing_demo_personas(session, container, scope)

            slip_count = 0
            fact_count = 0
            pencil_draft_count = 0
            reminder_count = 0
            voice_blob_count = 0

            for person_spec in personas:
                locked_rows = await people_repo.lock_user_persons_for_update(session, scope)
                entries = [people_repo.to_rank_entry(r) for r in locked_rows]
                new_rank, peer_updates = compute_new_person_rank(entries, tier=person_spec.tier)
                if peer_updates:
                    row_map = {r.id: r for r in locked_rows}
                    for pid, updated_rank in peer_updates.items():
                        if pid in row_map:
                            row_map[pid].rank = updated_rank
                            row_map[pid].updated_at = now

                person_id = new_id()
                await people_repo.create_person_with_aliases(
                    session,
                    scope,
                    dek,
                    person_id=person_id,
                    slug=person_spec.slug,
                    name_cn=person_spec.name_cn,
                    monogram=person_spec.monogram,
                    role_tag=person_spec.role_tag,
                    context_sub=person_spec.context_sub,
                    short_memo=person_spec.short_memo,
                    traits=list(person_spec.traits),
                    tier=person_spec.tier,
                    rank=new_rank,
                    state=PersonState.ACTIVE,
                    visual_tokens=default_visual_tokens(person_spec.tier),
                    extra_aliases=person_spec.aliases,
                    now=now,
                )

                for slip_idx, slip_spec in enumerate(person_spec.slips):
                    rec_id = new_id()
                    slip_local_dt = datetime.combine(
                        slip_spec.happened_on,
                        time(hour=10 + (slip_idx % 9), minute=(slip_idx * 7) % 60),
                        tzinfo=tz,
                    )
                    slip_utc = slip_local_dt.astimezone(UTC)

                    media_key: str | None = None
                    if slip_spec.has_demo_audio:
                        media_key = f"{scope.user_id}/records/{rec_id}.wav"
                        await container.storage.put_encrypted(
                            user_id=scope.user_id,
                            dek=dek,
                            object_key=media_key,
                            plaintext=wav_bytes,
                            content_type="audio/wav",
                        )
                        voice_blob_count += 1

                    snippet_text = _make_snippet(slip_spec.body)
                    rec_row = RecordRow(
                        id=rec_id,
                        user_id=scope.user_id,
                        person_id=person_id,
                        capture_id=None,
                        kind=slip_spec.kind.value,
                        title_enc=container.cipher.encrypt_str(
                            dek,
                            slip_spec.title,
                            user_id=scope.user_id,
                            table="records",
                            column="title_enc",
                            row_id=rec_id,
                        ),
                        body_enc=container.cipher.encrypt_str(
                            dek,
                            slip_spec.body,
                            user_id=scope.user_id,
                            table="records",
                            column="body_enc",
                            row_id=rec_id,
                        ),
                        snippet_enc=container.cipher.encrypt_str(
                            dek,
                            snippet_text,
                            user_id=scope.user_id,
                            table="records",
                            column="snippet_enc",
                            row_id=rec_id,
                        ),
                        media_object_key=media_key,
                        audio_duration_ms=slip_spec.audio_duration_ms,
                        stamp_code=slip_spec.stamp_code,
                        happened_on=slip_spec.happened_on,
                        pinned=slip_spec.pinned,
                        review_state=RecordReviewState.NORMAL.value,
                        version=1,
                        created_at=slip_utc,
                        updated_at=slip_utc,
                    )
                    session.add(rec_row)
                    await session.flush()
                    slip_count += 1

                    for fact_spec in slip_spec.facts:
                        fact_id = new_id()
                        fact_row = FactRow(
                            id=fact_id,
                            user_id=scope.user_id,
                            person_id=person_id,
                            record_id=rec_id,
                            capture_id=None,
                            type=fact_spec.fact_type.value,
                            polarity=fact_spec.polarity.value,
                            time_status=TimeStatus.CURRENT.value,
                            source_speaker=SourceSpeaker.NARRATOR.value,
                            content_enc=container.cipher.encrypt_str(
                                dek,
                                fact_spec.content,
                                user_id=scope.user_id,
                                table="facts",
                                column="content_enc",
                                row_id=fact_id,
                            ),
                            trigger_date=fact_spec.trigger_date,
                            is_lunar=fact_spec.is_lunar,
                            lunar_month=fact_spec.lunar_month,
                            lunar_day=fact_spec.lunar_day,
                            lunar_is_leap_month=fact_spec.lunar_is_leap_month,
                            status=FactStatus.CONFIRMED.value,
                            pending_reason=None,
                            needs_glance=fact_spec.needs_glance,
                            confidence=0.96,
                            created_by=CreatedBy.USER.value,
                            valid_at=slip_utc,
                            version=1,
                            created_at=slip_utc,
                            updated_at=slip_utc,
                        )
                        session.add(fact_row)
                        await session.flush()
                        fact_count += 1
                        if fact_spec.needs_glance:
                            pencil_draft_count += 1

                        if fact_spec.create_reminder:
                            rem = await upsert_reminder_for_fact(
                                session,
                                scope=scope,
                                dek=dek,
                                cipher=container.cipher,
                                fact=fact_row,
                                fact_content=fact_spec.content,
                                desired_status=ReminderStatus.ACTIVE,
                                now=now,
                                tz_name=tz_name,
                            )
                            if rem is not None:
                                reminder_count += 1

        for old_key in old_blobs_to_delete:
            await container.storage.delete_object(object_key=old_key)

        return DemoSeedSummary(
            email=normalized_email,
            user_id=user_id,
            created_new_user=created_new_user,
            reused_existing_demo_data=False,
            person_count=len(personas),
            slip_count=slip_count,
            fact_count=fact_count,
            pencil_draft_count=pencil_draft_count,
            reminder_count=reminder_count,
            voice_blob_count=voice_blob_count,
        )
    finally:
        await container.db.dispose()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed the MindWeave (心络) Demo Day showcase workspace with real RLS & AES-256-GCM encryption.",
    )
    parser.add_argument(
        "--email",
        default=DEFAULT_DEMO_EMAIL,
        help=f"Demo login email address (default: {DEFAULT_DEMO_EMAIL})",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Wipe existing demo-* personas for this user and re-seed fresh demo data.",
    )
    parser.add_argument(
        "--timezone",
        default="Asia/Shanghai",
        help="User timezone for reminder scheduling (default: Asia/Shanghai)",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    summary = asyncio.run(
        seed_demo_workspace(
            email=args.email,
            reset=args.reset,
            tz_name=args.timezone,
        )
    )
    print("\n=== MindWeave (心络) Demo Day Workspace Ready ===")
    print(f"  Demo Login Email : {summary.email}")
    print(f"  User ID          : {summary.user_id}")
    print(
        f"  Account Status   : {'provisioned new' if summary.created_new_user else 'reused existing'}"
    )
    if summary.reused_existing_demo_data:
        print(
            f"  Demo Personas    : {summary.person_count} already seeded "
            "(re-run with --reset for a fresh Demo Day snapshot)"
        )
    else:
        print(
            f"  Seeded Personas  : {summary.person_count} (妈妈 / 阿远 / 陈经理 / 4 位记繁琐联系人)"
        )
        print(f"  Journal Slips    : {summary.slip_count} (AES-256-GCM encrypted under FORCE RLS)")
        print(f"  Confirmed Facts  : {summary.fact_count}")
        print(f"  Live D1 Pencils  : {summary.pencil_draft_count} (ready to tap & ink!)")
        print(f"  Active Reminders : {summary.reminder_count}")
        print(f"  Voice Audio Blobs: {summary.voice_blob_count} (encrypted playable WAV)")
    print(
        "  Tip              : In dev mode, OTP autofills on the login screen for 1-click sign-in.\n"
    )


if __name__ == "__main__":
    main()
