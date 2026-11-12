"""Pure deterministic entity resolution and scoped vs global routing domain logic."""

from __future__ import annotations

import re
from collections.abc import Sequence
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
