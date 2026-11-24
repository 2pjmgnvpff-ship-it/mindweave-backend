"""Pure deterministic entity resolution and scoped vs global routing domain logic."""

from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from uuid import UUID

from mindweave.shared.enums import PersonState, RouteMode, Tier

_NON_PERSON_STOPWORDS = frozenset(
    {
        "",
        "ta",
        "他",
        "她",
        "它",
        "我",
        "你",
        "您",
        "自己",
        "本人",
        "咱",
        "咱们",
        "我们",
        "他们",
        "她们",
        "它们",
        "有人",
        "大家",
        "别人",
        "对方",
        "那位",
        "这个人",
        "这人",
        "某人",
        "target",
        "target_person",
        "今天",
        "明天",
        "昨天",
        "前天",
        "后天",
        "今晚",
        "今早",
        "刚才",
        "刚刚",
        "最近",
        "平时",
        "以前",
        "现在",
        "周末",
        "周末记",
        "下周",
        "上周",
        "这周",
        "本周",
        "下个月",
        "上个月",
        "这个月",
        "今年",
        "明年",
        "去年",
        "每天",
        "每次",
        "上午",
        "下午",
        "晚上",
        "中午",
        "凌晨",
        "半夜",
        "日常",
        "随手",
        "随手记",
        "备忘",
        "手札",
        "记录",
        "他说",
        "她说",
        "它说",
        "我说",
        "你说",
        "他严重",
        "她严重",
        "他不能",
        "她不能",
        "他喜欢",
        "她喜欢",
        "他不喜欢",
        "她不喜欢",
        "他讨厌",
        "她讨厌",
    }
)

_PRONOUN_STARTERS = ("他", "她", "它", "我", "你")
_TEMPORAL_MENTION_RE = re.compile(
    r"^(?:(?:周|星期|礼拜)[一二三四五六日天末].*|(?:上|下|本|这)(?:周|星期|礼拜|个月).*|\d{1,2}月(?:\d{1,2}[日号]?)?)$"
)

_DATE_LITERAL_RE = re.compile(
    r"\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}[日号]?|\d{1,2}月\d{1,2}[日号]?|\d{1,2}[:：]\d{2}"
)
_WORD_OR_CJK_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+", re.IGNORECASE)

_HISTORY_CLUE_STRIP_PHRASES: tuple[str, ...] = tuple(
    sorted(
        (
            "上次那个",
            "之前那个",
            "不喜欢吃",
            "不喜欢喝",
            "不爱吃",
            "不爱喝",
            "不能吃",
            "不能喝",
            "喜欢吃",
            "喜欢喝",
            "严重过敏",
            "改成了",
            "换成了",
            "下个月",
            "上个月",
            "这个月",
            "星期一",
            "星期二",
            "星期三",
            "星期四",
            "星期五",
            "星期六",
            "星期日",
            "星期天",
            "礼拜一",
            "礼拜二",
            "礼拜三",
            "礼拜四",
            "礼拜五",
            "礼拜六",
            "礼拜日",
            "礼拜天",
            "下周一",
            "下周二",
            "下周三",
            "下周四",
            "下周五",
            "下周六",
            "下周日",
            "下周天",
            "这周一",
            "这周二",
            "这周三",
            "这周四",
            "这周五",
            "这周六",
            "这周日",
            "这周天",
            "上周一",
            "上周二",
            "上周三",
            "上周四",
            "上周五",
            "上周六",
            "上周日",
            "上周天",
            "不喜欢",
            "周一",
            "周二",
            "周三",
            "周四",
            "周五",
            "周六",
            "周日",
            "周天",
            "周末",
            "下周",
            "上周",
            "这周",
            "本周",
            "今天",
            "明天",
            "昨天",
            "前天",
            "后天",
            "今晚",
            "明晚",
            "今早",
            "明早",
            "最近",
            "平时",
            "以前",
            "以后",
            "现在",
            "刚才",
            "刚刚",
            "上次",
            "下次",
            "这次",
            "那个",
            "这个",
            "事情",
            "东西",
            "时候",
            "时间",
            "问题",
            "一下",
            "一点",
            "一直",
            "总是",
            "已经",
            "其实",
            "本来",
            "原来",
            "记得",
            "提醒",
            "喜欢",
            "讨厌",
            "过敏",
            "忌口",
            "不吃",
            "不喝",
            "不能",
            "爱吃",
            "爱喝",
            "严重",
            "非常",
            "特别",
            "超级",
            "比较",
            "有点",
            "可能",
            "应该",
            "好像",
            "还是",
            "然后",
            "因为",
            "所以",
            "如果",
            "但是",
            "不过",
            "打算",
            "计划",
            "准备",
            "安排",
            "取消",
            "推迟",
            "改期",
            "改到",
            "改成",
            "改为",
            "换成",
            "提前",
            "延后",
            "上午",
            "下午",
            "晚上",
            "中午",
            "凌晨",
            "每天",
            "每次",
            "预约",
        ),
        key=len,
        reverse=True,
    )
)

_HISTORY_CLUE_SPLIT_CHARS = frozenset(
    "的了呀呢吧嘛哦啊哈很太最都也还又在把被给对和与及或且是就这那她他它我你您改"
)
_HISTORY_CLUE_LEADING_VERBS = "吃喝买带看做去来要总"
_HISTORY_CLUE_STOPWORDS: frozenset[str] = frozenset(
    {
        "喜欢",
        "讨厌",
        "过敏",
        "忌口",
        "不吃",
        "不喝",
        "不能",
        "爱吃",
        "爱喝",
        "最近",
        "今天",
        "明天",
        "昨天",
        "前天",
        "后天",
        "上周",
        "下周",
        "这周",
        "本周",
        "周末",
        "事情",
        "东西",
        "时候",
        "时间",
        "问题",
        "大家",
        "别人",
        "自己",
        "我们",
        "你们",
        "他们",
        "她们",
        "一下",
        "一点",
        "一直",
        "总是",
        "已经",
        "可能",
        "应该",
        "好像",
        "记得",
        "提醒",
        "准备",
        "打算",
        "计划",
        "安排",
        "平时",
        "以前",
        "以后",
        "现在",
        "刚才",
        "刚刚",
        "非常",
        "特别",
        "超级",
        "比较",
        "有点",
        "其实",
        "本来",
        "原来",
        "还是",
        "然后",
        "因为",
        "所以",
        "如果",
        "但是",
        "不过",
        "这个",
        "那个",
        "上次",
        "下次",
        "这次",
        "改到",
        "改成",
        "改为",
        "换成",
        "取消",
        "推迟",
        "提前",
        "改期",
        "每天",
        "每次",
        "上午",
        "下午",
        "晚上",
        "中午",
        "凌晨",
        "预约",
        "am",
        "pm",
        "ok",
        "no",
        "ta",
    }
)

# Spoken self-correction regexes (pure text hygiene for voice transcripts)
_SELF_CORRECTION_RE = re.compile(
    r"(?P<sep>[，,、；;\s]*)"
    r"(?:啊|哦|呃|嗯)?"
    r"(?:不对|说错了|讲错了|口误|搞错了|记错了|不是不是|不是)"
    r"(?:[，,、\s]*(?:应该是|其实是|改成|改到|是))"
    r"\s*(?P<replacement>[^，,。；;！!？?\n]+)"
)
_CLAUSE_BOUNDARY_RE = re.compile(r"[，,。；;！!？?\n]")
_CALENDAR_DATE_RE = re.compile(r"\d{1,2}月\d{1,2}[日号]?")
_FULL_WEEKDAY_RE = re.compile(r"(?:下|这|本|上)?(?:周|星期|礼拜)[一二三四五六日天末]")
_BARE_WEEKDAY_RE = re.compile(r"(?:周|星期|礼拜)[一二三四五六日天末]")
_RELATIVE_DAY_RE = re.compile(r"(?:大后天|今天|明天|后天|昨天|前天|今晚|明早|明晚)")
_CLOCK_TIME_RE = re.compile(
    r"(?:上午|下午|中午|晚上|早上|凌晨|傍晚)?"
    r"(?:\d{1,2}|[一二两三四五六七八九十]{1,2})[点时](?:半|\d{1,2}分?)?"
)
_DAY_PERIOD_RE = re.compile(r"(?:上午|下午|中午|晚上|早上|凌晨|傍晚)")
_PERSON_PREP_RE = re.compile(
    r"(?:和|跟|找|给|帮|陪|问|约)"
    r"((?:小|老|阿|大)[\u4e00-\u9fff]|"
    r"[\u4e00-\u9fff]{2,3}?(?=(?:一起|吃饭|喝|吃|去|看|买|送|做|聊|玩|见|说|打|开|学|走|住|谈|的|在|过|$)))"
)
_SUBJECT_PREDICATE_RE = re.compile(
    r"^([\u4e00-\u9fff]{2,3})"
    r"((?:喜欢|爱吃|爱喝|不吃|不喝|不能|讨厌|过敏|忌口|想去|想要|"
    r"下周|这周|上周|本周|明天|今天|后天|昨天|周[一二三四五六日天末]|星期[一二三四五六日天末]|"
    r"准备|打算|计划|预约|要去|去|说|提到|过生日|生日|体检|复查|结婚|纪念日|"
    r"住院|出差|搬家|换工作|升职|辞职|最近|平时|一直|总是|对).*)$"
)
_VERB_OBJECT_TAIL_RE = re.compile(
    r"(喜欢吃|喜欢喝|爱吃|爱喝|不能吃|不能喝|不爱吃|不爱喝|不吃|不喝|"
    r"喜欢|讨厌|想要|想吃|想喝|买|送|带|喝|吃|点)([\u4e00-\u9fffA-Za-z0-9]{1,8})$"
)
_PERSON_TITLE_SUFFIXES = (
    "妈",
    "爸",
    "哥",
    "姐",
    "弟",
    "妹",
    "叔",
    "姨",
    "婆",
    "公",
    "爷",
    "奶",
    "总",
    "老师",
    "医生",
    "经理",
    "主任",
    "同学",
    "同事",
    "师兄",
    "师姐",
)
_PERSON_NICK_PREFIXES = ("小", "老", "阿", "大")


def normalize_mention(text: str) -> str:
    return "".join(text.strip().lower().split())


def is_non_person_stopword_mention(mention: str) -> bool:
    """Return True if `mention` is a pronoun, temporal word, or pronoun+predicate clause."""
    norm = normalize_mention(mention)
    if norm in _NON_PERSON_STOPWORDS:
        return True
    if norm.startswith(_PRONOUN_STARTERS):
        return True
    return bool(_TEMPORAL_MENTION_RE.match(norm))


def is_pronoun_or_empty_mention(mention: str) -> bool:
    return is_non_person_stopword_mention(mention)


@dataclass(frozen=True, slots=True)
class KnownPersonProfile:
    person_id: UUID
    display_name: str
    aliases: tuple[str, ...] = ()
    tier: Tier = Tier.CRYSTAL
    state: PersonState = PersonState.ACTIVE
    role_tag: str = ""
    monogram: str = ""
    recent_facts: tuple[str, ...] = ()

    def matches_exact(self, normalized_query: str) -> bool:
        if not normalized_query:
            return False
        if normalize_mention(self.display_name) == normalized_query:
            return True
        return any(normalize_mention(alias) == normalized_query for alias in self.aliases)

    def all_surface_forms(self) -> tuple[str, ...]:
        forms = [self.display_name, *self.aliases]
        return tuple(f.strip() for f in forms if f and f.strip())


@dataclass(frozen=True, slots=True)
class MentionResolution:
    mention: str
    matched_person_id: UUID | None
    candidate_person_ids: tuple[UUID, ...] = ()
    is_new_person: bool = False
    is_cross_person_in_scoped_mode: bool = False
    scoped_locked: bool = False
    clause_person_unambiguous: bool = False


@dataclass(frozen=True, slots=True)
class CandidateHint:
    mention: str
    matched_person_id: UUID | None = None
    candidate_person_ids: tuple[UUID, ...] = field(default_factory=tuple)
    is_new_person: bool = False
    confidence: float = 0.9


def derive_monogram(display_name: str) -> str:
    cleaned = display_name.strip()
    if not cleaned:
        return "记"
    for ch in cleaned:
        if not ch.isspace():
            return ch[:1].upper()
    return "记"


def clause_mentions_multiple_known_people(
    clause_text: str,
    known_people: Sequence[KnownPersonProfile],
) -> bool:
    """Detect whether a single fact/clause text explicitly names 2+ distinct known people."""
    if not clause_text or len(known_people) < 2:
        return False
    hit_ids: set[UUID] = set()
    for person in known_people:
        for surface in person.all_surface_forms():
            if len(surface) >= 2 and surface in clause_text:
                hit_ids.add(person.person_id)
                break
    return len(hit_ids) >= 2


def _extract_history_clue_tokens(text: str) -> set[str]:
    """Extract distinctive 2-char CJK bigrams and 2+ char ASCII tokens, excluding generic stopwords."""
    cleaned = text.strip().lower()
    if not cleaned:
        return set()
    cleaned = _DATE_LITERAL_RE.sub(" ", cleaned)
    for phrase in _HISTORY_CLUE_STRIP_PHRASES:
        cleaned = cleaned.replace(phrase, " ")

    tokens: set[str] = set()
    for chunk in _WORD_OR_CJK_RE.findall(cleaned):
        if chunk.isascii():
            if len(chunk) >= 2 and not chunk.isdigit() and chunk not in _HISTORY_CLUE_STOPWORDS:
                tokens.add(chunk)
            continue
        # Split on structural particles/pronouns so unrelated adjacent words never fuse
        sub_buf: list[str] = []
        sub_chunks: list[str] = []
        for ch in chunk:
            if ch in _HISTORY_CLUE_SPLIT_CHARS:
                if sub_buf:
                    sub_chunks.append("".join(sub_buf))
                    sub_buf = []
            else:
                sub_buf.append(ch)
        if sub_buf:
            sub_chunks.append("".join(sub_buf))

        for sub in sub_chunks:
            core = sub
            while len(core) > 2 and core[0] in _HISTORY_CLUE_LEADING_VERBS:
                core = core[1:]
            if len(core) < 2:
                continue
            for idx in range(len(core) - 1):
                bigram = core[idx : idx + 2]
                if bigram not in _HISTORY_CLUE_STOPWORDS:
                    tokens.add(bigram)

    tokens -= _HISTORY_CLUE_STOPWORDS
    return tokens


def resolve_by_history_clues(
    clause_text: str,
    known_people: Sequence[KnownPersonProfile],
) -> MentionResolution | None:
    """Resolve an unnamed/pronoun global clause using distinctive overlap with recent confirmed facts.

    Returns:
    - A matched `MentionResolution` ONLY when a single person has a strictly unique match
      (`clause_person_unambiguous=True` when score >= 2 with zero overlap elsewhere;
      `clause_person_unambiguous=False` for moderate single-clue or margin matches so the
      slip still receives gentle user confirmation).
    - An ambiguous `MentionResolution` (`matched_person_id=None`, `candidate_person_ids=...`)
      when 2+ people share the historical clue without a clear margin — NEVER guessing blindly.
    - `None` when no distinctive historical clues overlap.
    """
    if not clause_text or not clause_text.strip() or not known_people:
        return None
    clause_tokens = _extract_history_clue_tokens(clause_text)
    if not clause_tokens:
        return None

    scored: list[tuple[int, KnownPersonProfile]] = []
    for person in known_people:
        if not person.recent_facts:
            continue
        person_tokens: set[str] = set()
        for fact in person.recent_facts:
            person_tokens.update(_extract_history_clue_tokens(fact))
        overlap = clause_tokens & person_tokens
        if overlap:
            scored.append((len(overlap), person))

    if not scored:
        return None

    scored.sort(key=lambda item: item[0], reverse=True)
    top_score, top_person = scored[0]

    if len(scored) == 1:
        return MentionResolution(
            mention=top_person.display_name,
            matched_person_id=top_person.person_id,
            candidate_person_ids=(top_person.person_id,),
            is_new_person=False,
            scoped_locked=False,
            clause_person_unambiguous=top_score >= 2,
        )

    second_score = scored[1][0]
    if top_score >= 3 and (top_score - second_score) >= 2 and top_score >= second_score * 2:
        return MentionResolution(
            mention=top_person.display_name,
            matched_person_id=top_person.person_id,
            candidate_person_ids=(top_person.person_id,),
            is_new_person=False,
            scoped_locked=False,
            clause_person_unambiguous=False,
        )

    return MentionResolution(
        mention="",
        matched_person_id=None,
        candidate_person_ids=tuple(p.person_id for _, p in scored),
        is_new_person=False,
        scoped_locked=False,
        clause_person_unambiguous=False,
    )


def _replace_last_match(pattern: re.Pattern[str], text: str, replacement: str) -> str | None:
    matches = list(pattern.finditer(text))
    if not matches:
        return None
    last = matches[-1]
    return f"{text[: last.start()]}{replacement}{text[last.end() :]}"


def _looks_like_person_name(text: str) -> bool:
    cleaned = text.strip()
    if not cleaned or len(cleaned) > 4:
        return False
    if any(cleaned.endswith(suf) for suf in _PERSON_TITLE_SUFFIXES):
        return True
    return len(cleaned) in (2, 3) and cleaned.startswith(_PERSON_NICK_PREFIXES)


def _apply_clause_replacement(clause: str, replacement: str) -> str | None:
    """Apply a spoken self-correction replacement inside the immediately preceding clause."""
    if not clause or not replacement:
        return None

    # 1. Calendar date (e.g. "5月12日" -> "5月15日")
    if _CALENDAR_DATE_RE.fullmatch(replacement):
        replaced = _replace_last_match(_CALENDAR_DATE_RE, clause, replacement)
        if replaced is not None:
            return replaced

    # 2. Weekday (e.g. "周五" -> "周六", "下周五" -> "下周六" or "下周二" -> "下周五")
    if _FULL_WEEKDAY_RE.fullmatch(replacement):
        if replacement[0] in ("下", "这", "本", "上"):
            replaced = _replace_last_match(_FULL_WEEKDAY_RE, clause, replacement)
        else:
            replaced = _replace_last_match(_BARE_WEEKDAY_RE, clause, replacement)
        if replaced is not None:
            return replaced

    # 3. Relative day (e.g. "明天" -> "后天")
    if _RELATIVE_DAY_RE.fullmatch(replacement):
        replaced = _replace_last_match(_RELATIVE_DAY_RE, clause, replacement)
        if replaced is not None:
            return replaced

    # 4. Clock time or day period (e.g. "3点" -> "4点", "上午" -> "下午")
    if _CLOCK_TIME_RE.fullmatch(replacement):
        replaced = _replace_last_match(_CLOCK_TIME_RE, clause, replacement)
        if replaced is not None:
            return replaced
    if _DAY_PERIOD_RE.fullmatch(replacement):
        replaced = _replace_last_match(_DAY_PERIOD_RE, clause, replacement)
        if replaced is not None:
            return replaced

    # 5. Compound noun sharing a 2-char (or 1-char) tail (e.g. "草莓蛋糕" -> "抹茶蛋糕")
    if len(replacement) >= 2 and all("\u4e00" <= ch <= "\u9fff" for ch in replacement):
        suffix_lengths = (2, 1) if len(replacement) >= 3 else (1,)
        leading_excluded = "买送带吃喝点给的看去来要很太最"
        for suf_len in suffix_lengths:
            suffix = replacement[-suf_len:]
            if suffix in _HISTORY_CLUE_SPLIT_CHARS:
                continue
            prefix_len = len(replacement) - suf_len
            candidate_lens = [prefix_len]
            if prefix_len > 1:
                candidate_lens.append(prefix_len - 1)
            candidate_lens.append(prefix_len + 1)
            for pre_len in candidate_lens:
                compound_re = re.compile(rf"[\u4e00-\u9fff]{{{pre_len}}}{re.escape(suffix)}")
                matches = [
                    m
                    for m in compound_re.finditer(clause)
                    if m.group(0) != replacement and m.group(0)[0] not in leading_excluded
                ]
                if matches:
                    last = matches[-1]
                    return f"{clause[: last.start()]}{replacement}{clause[last.end() :]}"

    # 6. Person / subject vs verb-object replacement
    is_person_cue = _looks_like_person_name(replacement)
    if is_person_cue:
        prep_matches = list(_PERSON_PREP_RE.finditer(clause))
        if prep_matches:
            last = prep_matches[-1]
            return f"{clause[: last.start(1)]}{replacement}{clause[last.end(1) :]}"
        subj_match = _SUBJECT_PREDICATE_RE.match(clause)
        if subj_match and not is_non_person_stopword_mention(subj_match.group(1)):
            return f"{replacement}{subj_match.group(2)}"

    verb_obj = _VERB_OBJECT_TAIL_RE.search(clause)
    if verb_obj and not is_person_cue:
        return f"{clause[: verb_obj.start(2)]}{replacement}"

    subj_match = _SUBJECT_PREDICATE_RE.match(clause)
    if (
        subj_match
        and 2 <= len(replacement) <= 4
        and not is_non_person_stopword_mention(subj_match.group(1))
    ):
        return f"{replacement}{subj_match.group(2)}"

    return None


_NEGATION_PIVOT_CORRECTION_RE = re.compile(
    r"(?P<sep>[，,、；;\s]*)"
    r"(?:啊|哦|呃|嗯)?[，,、\s]*"
    r"(?P<repair_prefix>(?:不对|搞错了|说错了|讲错了|口误|记错了)[，,、\s]*)?"
    r"(?:不是|并非)\s*(?P<rejected>[^，,。；;！!？?\n]{1,12}?)"
    r"[，,、\s]*(?P<connector>改成|改到|改为|换成|其实是|应该是|而是|是)"
    r"\s*(?P<replacement>[^，,。；;！!？?\n]+)"
)
_OVERRIDE_VERBS: frozenset[str] = frozenset({"改成", "改到", "改为", "换成"})
_WEEK_MODIFIERS: tuple[str, ...] = ("下", "这", "本", "上")

_TEMPORAL_WEEKDAY_PHRASE_RE = re.compile(
    r"(?:下|这|本|上)?(?:周|星期|礼拜)[一二三四五六日天末]"
    r"(?:(?:上午|下午|中午|晚上|早上|凌晨|傍晚)?"
    r"(?:(?:\d{1,2}|[一二两三四五六七八九十]{1,2})[点时](?:半|\d{1,2}分?)?)?)?"
)
_EXISTING_WEEKDAY_SPAN_RE = re.compile(
    r"(?:下|这|本|上)?(?:周|星期|礼拜)[一二三四五六日天末]"
    r"(?:上午|下午|中午|晚上|早上|凌晨|傍晚)?"
    r"(?:(?:\d{1,2}|[一二两三四五六七八九十]{1,2})[点时](?:半|\d{1,2}分?)?)?"
)
_PERSON_FOLLOWED_BY_PREDICATE_RE = re.compile(
    r"^(?P<person>(?:小|老|阿|大)[\u4e00-\u9fff]|[\u4e00-\u9fff]{2,3}?)"
    r"(?P<tail>(?:一起|吃晚饭|吃午饭|吃早饭|吃饭|喝|吃|去|看|买|送|做|聊|玩|见|说|打|开|学|走|住|谈).*)$"
)
_PERSON_PREP_TAIL_RE = re.compile(r"(?:和|跟|找|给|帮|陪|问|约)([\u4e00-\u9fff]{2,3})$")


def _split_prev_clause(before: str) -> tuple[str, str]:
    boundaries = list(_CLAUSE_BOUNDARY_RE.finditer(before))
    if boundaries:
        split_pos = boundaries[-1].end()
        return before[:split_pos], before[split_pos:]
    return "", before


def _apply_clause_replacement_v2(clause: str, replacement: str) -> str | None:
    if not clause or not replacement:
        return None
    # 0a. Weekday + optional period/clock phrase (e.g. "周三" -> "下周四晚上")
    if _TEMPORAL_WEEKDAY_PHRASE_RE.fullmatch(replacement):
        if replacement[0] in _WEEK_MODIFIERS:
            replaced = _replace_last_match(_EXISTING_WEEKDAY_SPAN_RE, clause, replacement)
        else:
            replaced = _replace_last_match(_BARE_WEEKDAY_RE, clause, replacement)
        if replaced is not None:
            return replaced
    # 0b. Person + trailing predicate (e.g. "刚跟阿哲，口误，是阿远吃晚饭" -> "刚跟阿远吃晚饭")
    pm = _PERSON_FOLLOWED_BY_PREDICATE_RE.match(replacement)
    if pm and _looks_like_person_name(pm.group("person")):
        prep_tail = _PERSON_PREP_TAIL_RE.search(clause)
        if prep_tail:
            return f"{clause[: prep_tail.start(1)]}{replacement}"
    return _apply_clause_replacement(clause, replacement)


def apply_spoken_self_correction(text: str) -> str:
    """Apply Chinese spoken self-correction hygiene without mutating clean text or contrast clauses.

    Handles natural voice repairs such as:
    - `"妈妈周五复查，不对，是周六"` -> `"妈妈周六复查"`
    - `"陈经理的评审会不是周三，改成下周四"` -> `"陈经理的评审会改成下周四"`
    - `"给林溪买草莓蛋糕，搞错了，不是草莓蛋糕，换成抹茶蛋糕"` -> `"给林溪买抹茶蛋糕"`
    - `"刚跟阿哲，口误，是阿远吃晚饭"` -> `"刚跟阿远吃晚饭"`
    Preserves contrastive facts verbatim (e.g. `"妈妈不是不吃辣，是不能吃海鲜"`).
    """
    if not text or not text.strip():
        return text

    result = text

    # Pass 1: Negation-pivot corrections ("不是X，改成/换成/是Y") with strict contrast guard
    for _ in range(5):
        changed = False
        for match in _NEGATION_PIVOT_CORRECTION_RE.finditer(result):
            rejected = match.group("rejected").strip()
            connector = match.group("connector").strip()
            has_repair_prefix = bool(match.group("repair_prefix"))
            has_override_verb = connector in _OVERRIDE_VERBS
            raw_rep = match.group("replacement").strip()
            rep_core = raw_rep.rstrip("吧哈啊呀哦啦呢了").strip() or raw_rep

            before = result[: match.start()]
            after = result[match.end() :]
            prefix_before_clause, prev_clause = _split_prev_clause(before)
            rejected_in_prev = bool(rejected and rejected in prev_clause)

            # CRITICAL GUARD: NEVER rewrite bare contrastive "不是X，是/而是Y"!
            if not (has_override_verb or has_repair_prefix or rejected_in_prev):
                continue

            if rejected_in_prev:
                idx = prev_clause.rfind(rejected)
                left = prev_clause[:idx]
                right = prev_clause[idx + len(rejected) :]
                if (
                    left.endswith(_WEEK_MODIFIERS)
                    and rep_core.startswith(_WEEK_MODIFIERS)
                    and rejected.startswith(("周", "星期", "礼拜"))
                ):
                    left = left[:-1]
                result = f"{prefix_before_clause}{left}{rep_core}{right}{after}"
                changed = True
                break

            if not before.strip():
                result = f"{rep_core}{after}"
                changed = True
                break

            leading_ws_len = len(prev_clause) - len(prev_clause.lstrip())
            leading_ws = prev_clause[:leading_ws_len]
            trimmed_clause = prev_clause[leading_ws_len:]
            corrected_clause = _apply_clause_replacement_v2(trimmed_clause, rep_core)
            if corrected_clause is not None:
                result = f"{prefix_before_clause}{leading_ws}{corrected_clause}{after}"
                changed = True
                break

            if has_override_verb:
                result = f"{before.rstrip('，,、 ')}{connector}{rep_core}{after}"
                changed = True
                break
        if not changed:
            break

    # Pass 2: Classic cue-only corrections ("不对/说错了/口误/记错了，改成/改到/是Y")
    for _ in range(5):
        changed = False
        for match in _SELF_CORRECTION_RE.finditer(result):
            raw_rep = match.group("replacement").strip()
            rep_core = raw_rep.rstrip("吧哈啊呀哦啦呢了").strip() or raw_rep
            before = result[: match.start()]
            after = result[match.end() :]
            prefix_before_clause, prev_clause = _split_prev_clause(before)
            leading_ws_len = len(prev_clause) - len(prev_clause.lstrip())
            leading_ws = prev_clause[:leading_ws_len]
            trimmed_clause = prev_clause[leading_ws_len:]
            corrected_clause = _apply_clause_replacement_v2(trimmed_clause, rep_core)
            if corrected_clause is None:
                continue
            result = f"{prefix_before_clause}{leading_ws}{corrected_clause}{after}"
            changed = True
            break
        if not changed:
            break

    return result


def partition_by_event_group[T](
    items: Sequence[T],
    get_event_group: Callable[[T], str | None],
) -> list[list[T]]:
    """Partition a person's facts into separate slips when 2+ distinct non-empty event groups exist.

    When `items` is empty, or all `event_group` values are None/empty or identical (fewer than 2
    distinct non-empty groups), preserves the exact single-`RecordRow` grouping.
    """
    if not items:
        return [[]]
    distinct_non_empty: set[str] = set()
    normalized: list[tuple[str, T]] = []
    for item in items:
        raw = get_event_group(item)
        key = raw.strip() if isinstance(raw, str) else ""
        if key:
            distinct_non_empty.add(key)
        normalized.append((key, item))
    if len(distinct_non_empty) < 2:
        return [list(items)]
    buckets: OrderedDict[str, list[T]] = OrderedDict()
    for key, item in normalized:
        buckets.setdefault(key, []).append(item)
    return list(buckets.values())


def resolve_mention_for_fact(
    *,
    mention: str,
    fact_person_id_hint: UUID | None,
    fact_content: str,
    full_transcript: str,
    route_mode: RouteMode,
    scoped_person_id: UUID | None,
    known_people: Sequence[KnownPersonProfile],
    candidate_hints: Sequence[CandidateHint] = (),
) -> MentionResolution:
    """Deterministically resolve a fact's target person under `current` or `global` mode.

    Priority hierarchy (BACKEND_MASTER_PLAN §3.1):
    1. `route_mode == CURRENT` hard-locks `scoped_person_id` for pronouns, empty mentions,
       or mentions of the scoped person. ONLY when a clause explicitly names ANOTHER known
       person whose name/alias appears verbatim in `full_transcript` does it split that
       fact into the other person's `pending` inbox (`is_cross_person_in_scoped_mode=True`).
    2. In `route_mode == GLOBAL`:
       a. Deterministic exact match against `display_name` + `aliases` (blind-index equivalent).
          - 1 match -> unambiguous known person.
          - 2+ matches -> `AMBIGUOUS_PERSON` (`matched_person_id=None`, `candidate_person_ids`).
       b. Validated LLM candidate hints (strictly filtered to `known_people` IDs for Invariant I1).
       c. Unknown non-pronoun human name -> new provisional `crystal` person (`is_new_person=True`).
    """
    by_id: dict[UUID, KnownPersonProfile] = {p.person_id: p for p in known_people}
    norm = normalize_mention(mention)

    # ------------------------------------------------------------------
    # Mode 1: Scoped (`route_mode == "current"`)
    # ------------------------------------------------------------------
    if route_mode is RouteMode.CURRENT:
        if scoped_person_id is None or scoped_person_id not in by_id:
            raise ValueError("scoped_person_id must reference a valid known person in current mode")
        scoped_profile = by_id[scoped_person_id]

        if is_pronoun_or_empty_mention(mention) or scoped_profile.matches_exact(norm):
            return MentionResolution(
                mention=mention or scoped_profile.display_name,
                matched_person_id=scoped_person_id,
                candidate_person_ids=(scoped_person_id,),
                is_new_person=False,
                is_cross_person_in_scoped_mode=False,
                scoped_locked=True,
                clause_person_unambiguous=True,
            )

        # Check if `mention` explicitly names ANOTHER known person AND appears in transcript
        other_exact = [
            p
            for p in known_people
            if p.person_id != scoped_person_id
            and p.matches_exact(norm)
            and any(surf in full_transcript for surf in p.all_surface_forms())
        ]
        if len(other_exact) == 1:
            other = other_exact[0]
            return MentionResolution(
                mention=mention,
                matched_person_id=other.person_id,
                candidate_person_ids=(other.person_id,),
                is_new_person=False,
                is_cross_person_in_scoped_mode=True,
                scoped_locked=False,
                clause_person_unambiguous=False,
            )
        if len(other_exact) > 1:
            return MentionResolution(
                mention=mention,
                matched_person_id=None,
                candidate_person_ids=tuple(p.person_id for p in other_exact),
                is_new_person=False,
                is_cross_person_in_scoped_mode=True,
                scoped_locked=False,
                clause_person_unambiguous=False,
            )

        # Never allow keyword hijacking in current mode: default back to scoped_person_id!
        return MentionResolution(
            mention=scoped_profile.display_name,
            matched_person_id=scoped_person_id,
            candidate_person_ids=(scoped_person_id,),
            is_new_person=False,
            is_cross_person_in_scoped_mode=False,
            scoped_locked=True,
            clause_person_unambiguous=True,
        )

    # ------------------------------------------------------------------
    # Mode 2: Global (`route_mode == "global"`)
    # ------------------------------------------------------------------
    entangled_clause = clause_mentions_multiple_known_people(fact_content, known_people)

    # 2a. Exact deterministic match on display_name / aliases
    if norm and not is_pronoun_or_empty_mention(mention):
        exact_matches = [p for p in known_people if p.matches_exact(norm)]
        if len(exact_matches) == 1:
            matched = exact_matches[0]
            return MentionResolution(
                mention=mention,
                matched_person_id=matched.person_id,
                candidate_person_ids=(matched.person_id,),
                is_new_person=False,
                scoped_locked=False,
                clause_person_unambiguous=not entangled_clause,
            )
        if len(exact_matches) >= 2:
            return MentionResolution(
                mention=mention,
                matched_person_id=None,
                candidate_person_ids=tuple(p.person_id for p in exact_matches),
                is_new_person=False,
                scoped_locked=False,
                clause_person_unambiguous=False,
            )

    # 2b. Look up matching LLM candidate hint for this mention
    hint: CandidateHint | None = None
    for h in candidate_hints:
        if normalize_mention(h.mention) == norm:
            hint = h
            break

    valid_hint_candidates: list[UUID] = []
    if hint is not None:
        for cid in hint.candidate_person_ids:
            if cid in by_id and cid not in valid_hint_candidates:
                valid_hint_candidates.append(cid)
        if (
            hint.matched_person_id is not None
            and hint.matched_person_id in by_id
            and hint.matched_person_id not in valid_hint_candidates
        ):
            valid_hint_candidates.insert(0, hint.matched_person_id)

    if len(valid_hint_candidates) >= 2:
        return MentionResolution(
            mention=mention,
            matched_person_id=None,
            candidate_person_ids=tuple(valid_hint_candidates),
            is_new_person=False,
            scoped_locked=False,
            clause_person_unambiguous=False,
        )

    if is_pronoun_or_empty_mention(mention):
        # Pronoun in global mode: check how many known people appear in transcript
        mentioned_in_transcript = [
            p
            for p in known_people
            if any(surf in full_transcript for surf in p.all_surface_forms() if surf)
        ]
        if len(mentioned_in_transcript) == 1:
            sole = mentioned_in_transcript[0]
            return MentionResolution(
                mention=sole.display_name,
                matched_person_id=sole.person_id,
                candidate_person_ids=(sole.person_id,),
                is_new_person=False,
                scoped_locked=False,
                clause_person_unambiguous=not entangled_clause,
            )
        if len(mentioned_in_transcript) >= 2:
            return MentionResolution(
                mention=mention,
                matched_person_id=None,
                candidate_person_ids=tuple(p.person_id for p in mentioned_in_transcript),
                is_new_person=False,
                scoped_locked=False,
                clause_person_unambiguous=False,
            )
        # No known person name appeared verbatim in full_transcript: check historical clues
        history_res = resolve_by_history_clues(fact_content, known_people)
        if history_res is None and full_transcript and full_transcript != fact_content:
            history_res = resolve_by_history_clues(full_transcript, known_people)
        if history_res is not None:
            if history_res.matched_person_id is not None:
                if (
                    fact_person_id_hint is not None
                    and fact_person_id_hint in by_id
                    and fact_person_id_hint != history_res.matched_person_id
                ):
                    return MentionResolution(
                        mention=mention,
                        matched_person_id=None,
                        candidate_person_ids=(history_res.matched_person_id, fact_person_id_hint),
                        is_new_person=False,
                        scoped_locked=False,
                        clause_person_unambiguous=False,
                    )
                return MentionResolution(
                    mention=history_res.mention,
                    matched_person_id=history_res.matched_person_id,
                    candidate_person_ids=history_res.candidate_person_ids,
                    is_new_person=False,
                    scoped_locked=False,
                    clause_person_unambiguous=history_res.clause_person_unambiguous
                    and (not entangled_clause),
                )
            return MentionResolution(
                mention=mention,
                matched_person_id=None,
                candidate_person_ids=history_res.candidate_person_ids,
                is_new_person=False,
                scoped_locked=False,
                clause_person_unambiguous=False,
            )
        if fact_person_id_hint is not None and fact_person_id_hint in by_id:
            return MentionResolution(
                mention=by_id[fact_person_id_hint].display_name,
                matched_person_id=fact_person_id_hint,
                candidate_person_ids=(fact_person_id_hint,),
                is_new_person=False,
                scoped_locked=False,
                clause_person_unambiguous=False,
            )
        # Unresolvable unnamed global note
        return MentionResolution(
            mention=mention,
            matched_person_id=None,
            candidate_person_ids=tuple(p.person_id for p in known_people),
            is_new_person=False,
            scoped_locked=False,
            clause_person_unambiguous=False,
        )

    # Non-pronoun mention that did NOT exact-match any known name/alias:
    # Check if hint explicitly flagged new person OR no valid known candidate matched
    if hint is not None and hint.is_new_person:
        return MentionResolution(
            mention=mention.strip(),
            matched_person_id=None,
            candidate_person_ids=(),
            is_new_person=True,
            scoped_locked=False,
            clause_person_unambiguous=False,
        )

    # Check partial surname+title ambiguous match (e.g. "老陈" when 陈明 & 陈叔 exist without alias)
    surname_char = re.sub(r"^(?:小|老|阿|大)", "", mention.strip())[:1]
    if surname_char and len(mention.strip()) <= 3:
        surname_matches = [
            p for p in known_people if p.display_name.strip().startswith(surname_char)
        ]
        if len(surname_matches) >= 2:
            return MentionResolution(
                mention=mention.strip(),
                matched_person_id=None,
                candidate_person_ids=tuple(p.person_id for p in surname_matches),
                is_new_person=False,
                scoped_locked=False,
                clause_person_unambiguous=False,
            )

    if (
        len(valid_hint_candidates) == 1
        and hint is not None
        and not hint.is_new_person
        and hint.matched_person_id is not None
    ):
        target_id = valid_hint_candidates[0]
        target_profile = by_id[target_id]
        # Only accept LLM single-candidate match if mention overlaps name/role; otherwise treat as new person!
        if (
            mention.strip() in target_profile.display_name
            or target_profile.display_name in mention.strip()
            or (target_profile.role_tag and mention.strip() in target_profile.role_tag)
        ):
            return MentionResolution(
                mention=mention.strip(),
                matched_person_id=target_id,
                candidate_person_ids=(target_id,),
                is_new_person=False,
                scoped_locked=False,
                clause_person_unambiguous=not entangled_clause,
            )

    # Otherwise, unknown human mention in global mode -> create provisional crystal person!
    return MentionResolution(
        mention=mention.strip(),
        matched_person_id=None,
        candidate_person_ids=(),
        is_new_person=True,
        scoped_locked=False,
        clause_person_unambiguous=False,
    )
