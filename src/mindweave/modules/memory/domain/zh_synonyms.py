"""Pure-domain Chinese relationship concept & synonym graph for encrypted hybrid search.

Strictly zero framework, database, HTTP, or ORM imports (enforced by `.importlinter`).
"""

from __future__ import annotations

CONCEPT_CLUSTERS: tuple[frozenset[str], ...] = (
    frozenset(
        {
            "保暖",
            "怕冷",
            "御寒",
            "秋裤",
            "羽绒",
            "羊绒",
            "护膝",
            "围巾",
            "暖宝",
            "保温",
            "受寒",
            "受凉",
            "着凉",
            "冬天",
            "加绒",
            "发凉",
        }
    ),
    frozenset(
        {
            "吵架",
            "争执",
            "矛盾",
            "冷战",
            "生气",
            "委屈",
            "误会",
            "别扭",
            "拌嘴",
            "道歉",
            "和好",
            "甩锅",
            "推诿",
            "证据",
            "留痕",
            "录音",
            "邮件",
            "截图",
            "改需求",
            "越界",
        }
    ),
    frozenset(
        {
            "医院",
            "复查",
            "体检",
            "吃药",
            "医生",
            "挂号",
            "膝盖",
            "骨科",
            "血压",
            "血糖",
            "胃疼",
            "手术",
            "片子",
            "医保",
            "过敏",
        }
    ),
    frozenset(
        {
            "礼物",
            "送礼",
            "想要",
            "喜欢",
            "种草",
            "鞋码",
            "尺码",
            "码数",
            "香水",
            "惊喜",
            "心愿",
            "买给",
            "围巾",
        }
    ),
    frozenset(
        {
            "忌口",
            "过敏",
            "不吃",
            "爱吃",
            "香菜",
            "辣椒",
            "甜食",
            "咖啡",
            "奶茶",
            "海鲜",
            "花生",
            "乳糖",
            "清淡",
            "低糖",
        }
    ),
    frozenset(
        {
            "生日",
            "阴历",
            "农历",
            "纪念",
            "周年",
            "蛋糕",
            "过生",
            "寿星",
            "蜡烛",
            "母亲",
            "父亲",
            "中秋",
            "过年",
        }
    ),
    frozenset(
        {
            "答应",
            "约定",
            "计划",
            "旅游",
            "旅行",
            "展览",
            "演唱",
            "吃饭",
            "见面",
            "下次",
            "下周",
            "联调",
        }
    ),
)

_CONCEPT_EXPANSION: dict[str, frozenset[str]] = {}
for _cluster in CONCEPT_CLUSTERS:
    for _term in _cluster:
        _existing = _CONCEPT_EXPANSION.get(_term, frozenset())
        _CONCEPT_EXPANSION[_term] = (_existing | _cluster) - {_term}


def expand_query_concepts(strong_terms: set[str], raw_query: str) -> set[str]:
    """Expand strong Chinese query terms via bidirectional life-domain concept clusters."""
    expanded: set[str] = set()
    q_low = raw_query.lower()
    for term in strong_terms:
        siblings = _CONCEPT_EXPANSION.get(term)
        if siblings:
            expanded.update(siblings)
    for key, siblings in _CONCEPT_EXPANSION.items():
        if len(key) >= 2 and key in q_low:
            expanded.add(key)
            expanded.update(siblings)
    expanded -= strong_terms
    return expanded
