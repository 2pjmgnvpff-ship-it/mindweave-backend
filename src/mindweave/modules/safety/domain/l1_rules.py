"""Pure deterministic L1 safety rule engine with hyperbole context exclusion (Invariant I5).

100% pure Python stdlib + shared enums; zero framework, DB, HTTP, or vendor SDK imports.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from mindweave.shared.enums import SafetyCategory


@dataclass(frozen=True, slots=True)
class L1SafetyLexicon:
    """Configurable linguistic feature patterns for L1 safety pre-screening."""

    # 1a. Hard lethal means / explicit suicide acts / overdose / farewell note (never suppressed by laughter)
    hard_crisis_patterns: tuple[str, ...] = (
        r"(?:自杀|轻生|结束自己的?生命|自我了断|寻短见)",
        r"(?:想|准备|去|打算|要|已经在?|站在[^。！？\n]{0,12})(?!原地起飞)(?:从(?:(?!原地起飞)[^。！？\n]){0,12})?(?:跳楼|跳下去|往下跳|跳河|跳江|跳海|上吊|服毒|吞药|吃安眠药)",
        r"活着(?:一点)?意义都没有",
        r"(?:安眠药|头孢配酒|烧炭自杀|割腕|划手腕)",
        r"(?:烧炭(?!烤|火锅)|炭盆(?!烤|火锅)|一氧化碳(?:中毒|自杀)?|吸一氧化碳)",
        r"(?:封死[^。！？\n]{0,16}门窗|门窗[^。！？\n]{0,12}封死)",
        r"(?:(?:封好|封上|胶带封)[^。！？\n]{0,16}门窗|门窗[^。！？\n]{0,12}(?:封好|封上|胶带封))[^。！？\n]{0,24}(?:走|睡|烧炭|点炭|炭|煤气|燃气|一氧化碳|解脱|告别)",
        r"(?:整瓶|一整瓶|大把|几十片|几盒|三盒)[^。！？\n]{0,12}(?:安眠药|镇静剂)",
        r"(?:吞|吞下|吞服|服下|全吞)[^。！？\n]{0,12}安眠药",
        r"(?:吃点?药|吞药|服药|攒了[^。！？\n]{0,16}药)[^。！？\n]{0,24}(?:睡过去|不醒|解脱|冰酒|配酒)",
        r"(?:药[^。！？\n]{0,16}(?:全吞下|整瓶吃|全吃下))",
        r"(?:永远不用醒|永远不会醒|再也不醒来?|长睡不醒)",
        r"(?:代写|润色|写|留)[^。！？\n]{0,16}(?:遗书|晚安告别留言|告别留言|告别信|绝笔信?)",
        r"(?:把[^。！？\n]{0,14}(?:记录|照片))[^。！？\n]{0,8}看最后一眼",
    )

    # 1b. Implicit farewell combination buckets (>= 2 distinct buckets => CRISIS)
    farewell_pet_or_dependent_patterns: tuple[str, ...] = (
        r"(?:猫|狗|宠物|毛孩子|金毛|猫咪|小狗)[^。！？\n]{0,30}(?:送到[^。！？\n]{0,12}(?:门口|家)|送去寄养|托付|领养|照顾|罐头|狗粮|猫粮)",
        r"(?:托付|拜托照顾)[^。！？\n]{0,16}(?:猫|狗|宠物|毛孩子)",
    )
    farewell_affairs_settlement_patterns: tuple[str, ...] = (
        r"(?:解锁密码|开机密码|银行卡密码|支付密码|支付宝密码|手机密码)[^。！？\n]{0,24}(?:写在|留在|记在|留给|信封|笔记本|便签)",
        r"(?:欠[^。！？\n]{0,10}钱|所有欠款|全部债务)[^。！？\n]{0,20}(?:转账清零|全部还清|转清|结清|清零)",
    )
    farewell_finality_patterns: tuple[str, ...] = (
        r"(?:过了今晚|今晚过后)[^。！？\n]{0,16}(?:彻底解脱|不用再痛苦|一了百了)",
        r"彻底解脱了",
        r"(?:看最后一眼|最后看一眼)[^。！？\n]{0,12}(?:照片|记录|大家|城市)",
    )

    # 1c. Colloquial figurative distress idioms (evaluated AFTER hyperbole context exclusion)
    ideation_crisis_patterns: tuple[str, ...] = (r"(?:不想活了|活不下去了|想原地去世|想死)",)

    # 1d. Colloquial workplace/study hyperbole context markers (anti-false-positive shield)
    mundane_stressor_patterns: tuple[str, ...] = (
        r"(?:PPT|改稿|第\s*\d+\s*版|加班|打工人|上班|oncall|早八|周会|需求|甲方|周报)",
        r"(?:论文|高数|期末|复习|考试|赶due|写作业|刷题)",
        r"(?:修\s*bug|线上\s*bug|写代码|服务器崩|堵车|减肥)",
    )
    joking_or_pivot_patterns: tuple[str, ...] = (
        r"(?:哈哈+|笑死|lol|hhh|233+|😂|🤣)",
        r"(?:言归正传|不过说正事|赶紧帮我记一下|顺便记一下|记一下[：:])",
    )

    # 2. Domestic violence / physical abuse / illegal detention SOS
    violence_victim_patterns: tuple[str, ...] = (
        r"(?:正在遭受|遭受)?(?:家暴|家庭暴力)",
        r"(?:动手打我|殴打我|掐我脖子|扇我耳光|拿刀威胁我|打得我浑身)",
        r"(?:把我锁在|锁了[^。！？\n]{0,8}大门)[^。！？\n]{0,18}(?:不让我出|不准出门|限制人身自由|不让走)",
        r"(?:非法拘禁|软禁我|抢走[^。！？\n]{0,8}手机[^。！？\n]{0,12}锁)",
    )

    # 3. Concrete offline illegal retaliation / crime plans (distinct from harmless verbal venting)
    illegal_intent_patterns: tuple[str, ...] = (
        r"(?:水杯|保温杯|杯子|咖啡|茶水|饭菜|食物|饮料)[^。！？\n]{0,20}(?:下药|投毒|洁厕灵|百草枯|泻药|老鼠药)",
        r"(?:往[^。！？\n]{0,12}倒点?[^。！？\n]{0,8}洁厕灵|下百草枯)",
        r"(?:剪断|剪掉|剪)[^。！？\n]{0,10}刹车线?",
        r"(?:拿着?钥匙|用钥匙|车库)[^。！？\n]{0,24}(?:车划烂|划烂[^。！？\n]{0,6}车|划花[^。！？\n]{0,6}车)",
        r"(?:买了?把?刀|买好弹簧刀|弹簧刀|水果刀|匕首|拿把刀|带把刀|持刀|拎着钢管)[^。！？\n]{0,24}(?:楼下蹲|楼下堵|家门口堵|公司楼下|砍|捅)",
        r"在(?:他|她)家楼下堵(?:他|她)[^。！？\n]{0,16}刀",
        r"(?:私密照|私密照片|裸照|私密裸照|不雅视频)[^。！？\n]{0,24}(?:发到[^。！？\n]{0,12}群|群发|发网上|曝光)",
        r"(?:(?:匿名举报信|勒索信)[^。！？\n]{0,24}(?:敲诈|封口费)|敲诈(?:暗示|勒索|话术|他|她))",
    )

    # 4. Third-party scoring / lie-detector / NPD diagnosis / soft-spot profiling / public doxxing
    third_party_attack_patterns: tuple[str, ...] = (
        r"(?:测谎仪|测谎报告|言行一致性测谎|CDI|欺骗指数|撒谎(?:白嫖)?概率|海王概率|渣男指数|渣男打分|打个?信用分|0到100分[^。！？\n]{0,18}打分)",
        r"(?:人情账[^。！？\n]{0,10}盈亏|爱的语系夹角|情感操控危险度|白嫖指数)",
        r"(?:对照\s*DSM[\s-]*5|诊断[^。！？\n]{0,20}(?:NPD|自恋型人格障碍|回避型依恋|PUA))",
        r"(?:99%[^。！？\n]{0,8}海王|惯犯骗子|致命软肋|可拿捏软肋)[^。！？\n]{0,24}(?:存进|档案|核心偏好|记下来|拿捏)",
        r"(?:小红书|朋友圈)[^。！？\n]{0,24}(?:曝光(?:她|他)|挂人|避雷贴|罪状鉴定书)",
        r"(?:最恶毒的脏话骂死|字字诛心[^。！？\n]{0,20}崩溃内疚)",
        r"(?:煤气灯效应|Gaslighting|暗黑[^。！？\n]{0,10}毒舌军师)[^。！？\n]{0,28}(?:操控话术|摧毁[^。！？\n]{0,8}自信)",
        r"(?:剪碎扔进垃圾站|销毁[^。！？\n]{0,8}财物)[^。！？\n]{0,24}(?:逼他低头|十秒钟不回|分手)",
    )


@dataclass(frozen=True, slots=True)
class L1SafetyResult:
    """Deterministic L1 classification output (zero numeric risk scores per R13)."""

    category: SafetyCategory
    matched_rule: str = "normal"
    is_hyperbole_excluded: bool = False


_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]+")
_INTRA_CJK_SEP_RE = re.compile(r"(?<=[\u4e00-\u9fff])[\s\u3000·•\u30fb._-]+(?=[\u4e00-\u9fff])")


def _normalize_safety_inputs(text: str) -> tuple[str, str]:
    """Return `(raw_stripped, cjk_collapsed)` with zero-width formatting and intra-CJK separators removed."""
    raw_stripped = _ZERO_WIDTH_RE.sub("", text or "").strip()
    if not raw_stripped:
        return "", ""
    cjk_collapsed = _INTRA_CJK_SEP_RE.sub("", raw_stripped)
    return raw_stripped, cjk_collapsed


def _matches_any(patterns: tuple[str, ...], text: str) -> bool:
    return any(re.search(pat, text, flags=re.IGNORECASE) for pat in patterns)


def _matches_dual(patterns: tuple[str, ...], raw_text: str, normalized_text: str) -> bool:
    if _matches_any(patterns, raw_text):
        return True
    return normalized_text != raw_text and _matches_any(patterns, normalized_text)


class L1SafetyEngine:
    """Deterministic first-layer safety classifier with anti-false-positive context exclusion."""

    def __init__(self, lexicon: L1SafetyLexicon | None = None) -> None:
        self._lexicon = lexicon or L1SafetyLexicon()

    def evaluate(self, text: str) -> L1SafetyResult:
        cleaned, normalized = _normalize_safety_inputs(text)
        if not cleaned:
            return L1SafetyResult(category=SafetyCategory.NORMAL, matched_rule="empty")

        lex = self._lexicon

        # 1. Domestic violence / immediate physical danger SOS
        if _matches_dual(lex.violence_victim_patterns, cleaned, normalized):
            return L1SafetyResult(
                category=SafetyCategory.VIOLENCE_VICTIM,
                matched_rule="domestic_violence_sos",
            )

        # 2. Hard lethal means / overdose / farewell note (never suppressed by hyperbole)
        if _matches_dual(lex.hard_crisis_patterns, cleaned, normalized):
            return L1SafetyResult(
                category=SafetyCategory.CRISIS,
                matched_rule="explicit_lethal_self_harm",
            )

        # 3. Multi-signal implicit farewell arrangement (>= 2 buckets => CRISIS)
        farewell_buckets = sum(
            (
                1
                if _matches_dual(lex.farewell_pet_or_dependent_patterns, cleaned, normalized)
                else 0,
                1
                if _matches_dual(lex.farewell_affairs_settlement_patterns, cleaned, normalized)
                else 0,
                1 if _matches_dual(lex.farewell_finality_patterns, cleaned, normalized) else 0,
            )
        )
        if farewell_buckets >= 2:
            return L1SafetyResult(
                category=SafetyCategory.CRISIS,
                matched_rule="implicit_farewell_combo",
            )

        # 4. Colloquial workplace/study hyperbole exclusion BEFORE general ideation triggers
        has_ideation = _matches_dual(lex.ideation_crisis_patterns, cleaned, normalized)
        if has_ideation:
            has_stressor = _matches_dual(lex.mundane_stressor_patterns, cleaned, normalized)
            has_joke_or_pivot = _matches_dual(lex.joking_or_pivot_patterns, cleaned, normalized)
            if has_stressor and has_joke_or_pivot:
                # Continue checking non-crisis categories (in case they also asked to score someone),
                # while guaranteeing zero crisis false-positive!
                if _matches_dual(lex.illegal_intent_patterns, cleaned, normalized):
                    return L1SafetyResult(
                        category=SafetyCategory.ILLEGAL_INTENT,
                        matched_rule="concrete_illegal_plan",
                        is_hyperbole_excluded=True,
                    )
                if _matches_dual(lex.third_party_attack_patterns, cleaned, normalized):
                    return L1SafetyResult(
                        category=SafetyCategory.THIRD_PARTY_ATTACK,
                        matched_rule="third_party_scoring_or_attack",
                        is_hyperbole_excluded=True,
                    )
                return L1SafetyResult(
                    category=SafetyCategory.NORMAL,
                    matched_rule="colloquial_hyperbole_excluded",
                    is_hyperbole_excluded=True,
                )
            return L1SafetyResult(
                category=SafetyCategory.CRISIS,
                matched_rule="explicit_suicidal_ideation",
            )

        # 5. Concrete offline illegal retaliation plans (harmless venting stays normal!)
        if _matches_dual(lex.illegal_intent_patterns, cleaned, normalized):
            return L1SafetyResult(
                category=SafetyCategory.ILLEGAL_INTENT,
                matched_rule="concrete_illegal_plan",
            )

        # 6. Third-party scoring / lie-detection / clinical labeling / doxxing / PUA manipulation
        if _matches_dual(lex.third_party_attack_patterns, cleaned, normalized):
            return L1SafetyResult(
                category=SafetyCategory.THIRD_PARTY_ATTACK,
                matched_rule="third_party_scoring_or_attack",
            )

        return L1SafetyResult(category=SafetyCategory.NORMAL, matched_rule="normal")

    def classify(self, text: str) -> SafetyCategory:
        return self.evaluate(text).category
