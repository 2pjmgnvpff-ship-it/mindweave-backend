"""Deterministic post-ASR Chinese homophone restoration and spoken self-correction helper.

Applied after ASR providers (both real multimodal LLMs and offline FakeAdapter) as a
conservative, idempotent safety net:
1. Resolves mid-sentence spoken self-corrections (e.g. "约了周五，不对，是周六" -> "约了周六",
   "刚跟阿哲，说错了是阿远，吃晚饭" -> "刚跟阿远吃晚饭") without fabricating facts.
2. Restores obvious single-character Chinese homophone misrecognitions ONLY when verified
   against `active_person_name`, `known_person_names`, or `recent_context_snippets`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

# Curated Mandarin & Southern-accent ASR homophone/near-homophone equivalence groups
# (exact homophones, flat/retroflex z/zh c/ch s/sh, front/back nasal n/ng).
_HOMOPHONE_GROUPS: tuple[str, ...] = (
    "陈程晨辰沉城成诚承澄",
    "林琳霖临邻凌玲铃灵菱",
    "屿雨宇羽语玉郁予瑜榆渔余于俞虞",
    "棠唐糖堂汤",
    "晚婉宛挽碗万湾",
    "远源袁园元原圆媛缘",
    "龙笼隆拢聋",
    "绒容荣融蓉溶",
    "张章彰樟",
    "周舟州洲邹",
    "李黎莉丽利力立礼栗理",
    "王汪旺望",
    "刘柳留流琉",
    "赵照兆昭",
    "孙损笋",
    "吴武伍舞午乌巫屋",
    "徐许叙旭绪栩须虚",
    "沈审慎申深盛胜圣",
    "清青轻晴情庆琴秦勤钦",
    "秋丘邱球求",
    "哲折浙泽责",
    "杰洁捷结姐节界",
    "放方芳房防访",
    "夏霞峡侠吓下",
    "知志智芝之枝织职直",
    "围维唯威微伟卫魏味",
    "巾金今津斤进静晶京景敬经",
    "膝溪希熙曦西惜夕席习",
    "盖改概钙",
    "排牌拍派",
    "骨谷古鼓顾固姑",
    "签千牵谦钱前浅倩",
    "字自紫子姿资滋",
    "茶查察差插",
    "茉莫墨末默磨",
    "桂贵归规硅",
    "芒忙盲茫",
    "香乡箱湘祥翔享想向",
    "菜采彩财才猜",
    "拿那娜纳",
    "铁贴帖",
    "燕颜严言研岩炎烟盐延",
    "麦卖买迈脉",
    "花华划画话化",
    "桃陶淘涛逃",
    "藕偶欧鸥",
    "蟹谢写协鞋",
    "妈麻马玛码",
    "爸巴吧八芭",
    "医一依伊仪宜艺易意忆益怡逸",
    "复副富付傅福扶符",
    "佳家嘉加夹假",
    "明铭鸣名茗敏闵",
    "浩豪昊好号皓",
    "涵寒韩汗汉含航",
    "欣心新鑫馨辛星兴幸",
    "思斯丝司师诗时实石史",
    "然燃染",
    "轩萱宣旋玄选",
    "宁凝柠",
    "梦孟萌蒙",
    "文雯温闻稳",
    "博波伯柏泊",
    "杨洋阳羊养样",
    "黄皇煌晃",
    "何贺河和荷核盒",
    "高糕膏告",
    "郭国果过锅",
    "罗洛落螺萝",
    "郑正政争曾",
    "宋松送颂",
    "苏素酥速宿",
    "陆路露录鹿",
    "晓小筱肖萧潇",
    "星兴猩惺",
    "河和禾荷",
    "梨离璃厘黎丽",
    "莓梅玫眉媒",
    "蓝兰岚拦篮",
    "桂瑰贵规",
    "椒焦胶娇骄",
    "辣腊蜡拉",
    "姜江将疆浆",
    "蒜算酸",
    "虾瞎霞峡",
    "奶乃耐奈",
    "咖卡喀",
    "啡非飞菲肥",
)

_CHAR_TO_GROUPS: dict[str, frozenset[int]] = {}
for _gid, _grp in enumerate(_HOMOPHONE_GROUPS):
    for _ch in _grp:
        _existing = _CHAR_TO_GROUPS.get(_ch, frozenset())
        _CHAR_TO_GROUPS[_ch] = _existing | frozenset({_gid})

# Characters that must never be overwritten as the single differing character in homophone fixes
_PROTECTED_DIFF_CHARS: frozenset[str] = frozenset(
    "0123456789一二三四五六七八九十零两百千万"
    "不没别非无未否"
    "我你您他她它咱"
    "的了着过地得在是有和与或吗呢吧啊呀哦嗯"
    "年月日号周点分秒早晚"
)

_SELF_CORRECTION_CUE_RE = re.compile(
    r"[，,、\s]*(?:呃|哦|哎|啊)?[，,、\s]*(?:不对|说错了|搞错了|口误)[，,、\s]*(?:应该)?(?:是)?[，,、\s]*"
)
_EVALUATIVE_BEFORE_CUE_RE = re.compile(r"(?:觉得|感觉|这样|那样|有点|好像|真的?|太|很|并|也)$")

_WEEKDAY_RE = re.compile(r"(?:这周|本周|下周|上周|周|星期)[一二三四五六日天末]")
_DATE_RE = re.compile(
    r"(?:\d{1,2}月)?\d{1,2}[日号]|"
    r"(?:[一二三四五六七八九十]+月)?(?:初[一二三四五六七八九十]|[一二三四五六七八九十]+[日号])"
)
_TIME_RE = re.compile(
    r"(?:上午|下午|早上|晚上|明早|明晚)?(?:\d{1,2}|[一二三四五六七八九十两]+)[点时](?:半|\d{1,2}分)?"
)
_PREP_BEFORE_NAME_RE = re.compile(
    r"(?:^|[，,。！？；;\s]|(?:跟|给|和|同|找|叫|让|把|帮|替|问|对|是|约|陪))([\u4e00-\u9fa5]{2,3})$"
)
_VERB_BEFORE_PHRASE_RE = re.compile(
    r"(?:跟|给|和|找|叫|让|把|帮|买了?|喝了?|吃了?|点了?|送了?|订了?|去|是)([\u4e00-\u9fa5]{2,4})$"
)

_CONTEXT_STOPWORDS: frozenset[str] = frozenset(
    {
        "今天",
        "明天",
        "昨天",
        "最近",
        "上次",
        "这次",
        "下次",
        "喜欢",
        "讨厌",
        "觉得",
        "可以",
        "应该",
        "已经",
        "还是",
        "就是",
        "因为",
        "所以",
        "一起",
        "一下",
        "记得",
        "提醒",
        "平时",
        "经常",
        "总是",
        "非常",
        "特别",
        "比较",
        "严重",
    }
)


def are_homophonic(ch_a: str, ch_b: str) -> bool:
    """Return True when two CJK characters belong to the same Mandarin/ASR phonetic group."""
    if ch_a == ch_b:
        return True
    g_a = _CHAR_TO_GROUPS.get(ch_a)
    g_b = _CHAR_TO_GROUPS.get(ch_b)
    if not g_a or not g_b:
        return False
    return bool(g_a & g_b)


def _replace_last_span(
    before: str,
    pattern: re.Pattern[str],
    replacement: str,
    *,
    max_lookback: int,
) -> str | None:
    matches = list(pattern.finditer(before))
    if not matches:
        return None
    last_m = matches[-1]
    if len(before) - last_m.end() > max_lookback:
        return None
    return before[: last_m.start()] + replacement + before[last_m.end() :]


def _clean_join_after(after_rest: str, *, absorb_leading_comma: bool) -> str:
    if absorb_leading_comma:
        return re.sub(r"^[，,、\s]+", "", after_rest)
    return after_rest


def resolve_spoken_self_corrections(
    text: str,
    *,
    candidate_names: Sequence[str] = (),
) -> str:
    """Resolve Chinese spoken self-corrections cleanly without inventing facts."""
    result = text
    sorted_names = sorted(
        {n.strip() for n in candidate_names if len(n.strip()) >= 2},
        key=len,
        reverse=True,
    )

    for _ in range(4):
        changed = False
        for m in _SELF_CORRECTION_CUE_RE.finditer(result):
            before = result[: m.start()]
            after = result[m.end() :]
            if not before or not after:
                continue
            if _EVALUATIVE_BEFORE_CUE_RE.search(before.rstrip()):
                continue

            # 1. Weekday self-correction ("约了周五，不对，是周六" / "下周三复查，不对，是下周六")
            wd_m = _WEEKDAY_RE.match(after)
            if wd_m is not None:
                replaced = _replace_last_span(before, _WEEKDAY_RE, wd_m.group(0), max_lookback=6)
                if replaced is not None:
                    rest = _clean_join_after(after[wd_m.end() :], absorb_leading_comma=False)
                    result = replaced + rest
                    changed = True
                    break

            # 2. Calendar date self-correction ("12号，不对，是15号")
            dt_m = _DATE_RE.match(after)
            if dt_m is not None:
                replaced = _replace_last_span(before, _DATE_RE, dt_m.group(0), max_lookback=6)
                if replaced is not None:
                    rest = _clean_join_after(after[dt_m.end() :], absorb_leading_comma=False)
                    result = replaced + rest
                    changed = True
                    break

            # 3. Clock time self-correction ("下午三点，不对，是下午四点")
            tm_m = _TIME_RE.match(after)
            if tm_m is not None:
                replaced = _replace_last_span(before, _TIME_RE, tm_m.group(0), max_lookback=6)
                if replaced is not None:
                    rest = _clean_join_after(after[tm_m.end() :], absorb_leading_comma=False)
                    result = replaced + rest
                    changed = True
                    break

            # 4. Known person name self-correction ("刚跟阿哲，说错了是阿远，吃晚饭")
            matched_known = next((nm for nm in sorted_names if after.startswith(nm)), None)
            if matched_known is not None:
                replaced_person: str | None = None
                for old_nm in sorted_names:
                    if old_nm != matched_known and before.endswith(old_nm):
                        replaced_person = before[: -len(old_nm)] + matched_known
                        break
                if replaced_person is None:
                    prep_m = _PREP_BEFORE_NAME_RE.search(before)
                    if prep_m is not None:
                        old_tok = prep_m.group(1)
                        if old_tok != matched_known:
                            replaced_person = before[: prep_m.start(1)] + matched_known
                if replaced_person is not None:
                    rest = _clean_join_after(
                        after[len(matched_known) :],
                        absorb_leading_comma=True,
                    )
                    result = replaced_person + rest
                    changed = True
                    break

            # 5. Generic parallel entity/phrase self-correction (same length after verb/prep)
            phrase_m = re.match(r"([\u4e00-\u9fa5]{2,4})(?=[，,。！？；;\s]|$)", after)
            if phrase_m is not None:
                new_phrase = phrase_m.group(1)
                verb_m = _VERB_BEFORE_PHRASE_RE.search(before)
                if verb_m is not None:
                    old_phrase = verb_m.group(1)
                    if (
                        len(old_phrase) == len(new_phrase)
                        and old_phrase != new_phrase
                        and old_phrase not in _CONTEXT_STOPWORDS
                    ):
                        rest = _clean_join_after(
                            after[phrase_m.end() :],
                            absorb_leading_comma=True,
                        )
                        result = before[: verb_m.start(1)] + new_phrase + rest
                        changed = True
                        break

        if not changed:
            break

    return result


def _extract_context_terms(snippets: Sequence[str]) -> list[str]:
    """Extract clean CJK candidate vocabulary n-grams (len 2..5) from recent context snippets."""
    terms: list[str] = []
    seen: set[str] = set()
    for snippet in snippets:
        for run in re.findall(r"[\u4e00-\u9fa5]{2,}", snippet):
            max_n = min(5, len(run))
            for n in range(max_n, 1, -1):
                for idx in range(0, len(run) - n + 1):
                    cand = run[idx : idx + n]
                    if cand in seen or cand in _CONTEXT_STOPWORDS:
                        continue
                    if cand[0] in _PROTECTED_DIFF_CHARS or cand[-1] in _PROTECTED_DIFF_CHARS:
                        continue
                    seen.add(cand)
                    terms.append(cand)
    terms.sort(key=len, reverse=True)
    return terms


def _apply_homophone_term(
    text: str,
    target: str,
    *,
    protected_exact_terms: frozenset[str],
) -> str:
    t_len = len(target)
    if t_len < 2 or len(text) < t_len:
        return text

    chars = list(text)
    idx = 0
    while idx <= len(chars) - t_len:
        window = "".join(chars[idx : idx + t_len])
        if window == target or window in protected_exact_terms:
            idx += 1
            continue
        diffs: list[tuple[str, str]] = []
        exact_matches = 0
        for w_ch, t_ch in zip(window, target, strict=True):
            if w_ch == t_ch:
                exact_matches += 1
            else:
                diffs.append((w_ch, t_ch))
        if (
            len(diffs) == 1
            and exact_matches >= 1
            and diffs[0][0] not in _PROTECTED_DIFF_CHARS
            and diffs[0][1] not in _PROTECTED_DIFF_CHARS
            and are_homophonic(diffs[0][0], diffs[0][1])
        ):
            chars[idx : idx + t_len] = list(target)
            idx += t_len
            continue
        idx += 1
    return "".join(chars)


def restore_context_homophones(
    text: str,
    *,
    active_person_name: str | None = None,
    known_person_names: Sequence[str] = (),
    recent_context_snippets: Sequence[str] = (),
) -> str:
    """Fix obvious Chinese homophone errors ONLY using provided person names & recent context."""
    if not text.strip():
        return text

    person_terms: list[str] = []
    seen_p: set[str] = set()
    for raw in ([active_person_name] if active_person_name else []) + list(known_person_names):
        cleaned = raw.strip()
        if len(cleaned) >= 2 and cleaned not in seen_p:
            seen_p.add(cleaned)
            person_terms.append(cleaned)
    person_terms.sort(key=len, reverse=True)

    protected_exact = frozenset(person_terms)
    out = text
    for term in person_terms:
        out = _apply_homophone_term(out, term, protected_exact_terms=protected_exact)

    if recent_context_snippets:
        context_terms = _extract_context_terms(recent_context_snippets)
        for term in context_terms:
            if len(term) < 3:
                continue
            out = _apply_homophone_term(out, term, protected_exact_terms=protected_exact)

    return out


def postprocess_asr_transcript(
    transcript: str,
    *,
    active_person_name: str | None = None,
    known_person_names: Sequence[str] = (),
    recent_context_snippets: Sequence[str] = (),
) -> str:
    """Idempotent deterministic post-ASR cleaner: homophone grounding + spoken self-correction."""
    cleaned = re.sub(r"\s+", " ", transcript or "").strip()
    if not cleaned:
        return ""

    candidate_names: list[str] = []
    if active_person_name and active_person_name.strip():
        candidate_names.append(active_person_name.strip())
    candidate_names.extend(n.strip() for n in known_person_names if n.strip())

    # 1. Restore homophones first so self-correction recognizes known person names cleanly
    grounded = restore_context_homophones(
        cleaned,
        active_person_name=active_person_name,
        known_person_names=known_person_names,
        recent_context_snippets=recent_context_snippets,
    )
    # 2. Resolve mid-sentence spoken self-corrections
    resolved = resolve_spoken_self_corrections(grounded, candidate_names=candidate_names)
    return re.sub(r"\s+", " ", resolved).strip()
