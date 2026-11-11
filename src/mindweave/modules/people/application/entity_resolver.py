"""Deterministic-first EntityResolver & KnownPersonRef loader (Invariant I1, I3, I12).

Resolution Priority Order:
1. `route_mode == RouteMode.CURRENT` + `scoped_person_id`: hard-locks `scoped_person_id`
   unless the clause explicitly names a DIFFERENT known person.
2. Exact blind-index HMAC lookup on `person_aliases` (`alias_hmac`) across non-deleted,
   non-archived persons -> 1 match = high-confidence; >1 matches = homonym disambiguation
   (`candidate_person_ids`, `ambiguous=True`).
3. Substring / mention match against decrypted `KnownPersonRef` list (`list_known_persons`).
4. In `RouteMode.GLOBAL`, auto-provisions a `provisional` `crystal` person
   (`create_provisional_person`) when an unknown person name is mentioned.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.ai.schemas.contracts import KnownPersonRef
from mindweave.core.clock import Clock, SystemClock
from mindweave.core.crypto import AliyunKmsStubProvider, EnvelopeCipher, normalize_alias_for_index
from mindweave.core.dependencies import AppContainer
from mindweave.core.ids import new_id
from mindweave.core.orm import PersonRow
from mindweave.modules.people.domain.copy_policy import (
    MAX_NAME_CN_LEN,
    MAX_ROLE_TAG_LEN,
    derive_monogram,
    find_forbidden_phrase,
    validate_optional_copy_field,
)
from mindweave.modules.people.domain.entities import default_visual_tokens
from mindweave.modules.people.domain.tier_transition import (
    compute_new_person_rank,
    partition_person_ids,
)
from mindweave.modules.people.infra.repository import PeopleRepository
from mindweave.shared.enums import PersonState, RouteMode, Tier
from mindweave.shared.scopes import UserScope

_GENERIC_PRONOUNS: frozenset[str] = frozenset(
    {
        "",
        "他",
        "她",
        "它",
        "ta",
        "他们",
        "她们",
        "我",
        "自己",
        "本人",
        "我们",
        "对方",
        "那个人",
        "这个人",
        "此人",
        "当前人物",
        "有人",
        "大家",
        "未知",
        "unknown",
        "none",
        "null",
    }
)


def _is_generic_pronoun(text: str) -> bool:
    norm = normalize_alias_for_index(text)
    return norm in _GENERIC_PRONOUNS


def _is_plausible_new_person_name(text: str) -> bool:
    cleaned = text.strip()
    if not cleaned or len(cleaned) > MAX_NAME_CN_LEN:
        return False
    if _is_generic_pronoun(cleaned):
        return False
    if find_forbidden_phrase(cleaned) is not None:
        return False
    return any(ch.isalpha() or ("\u4e00" <= ch <= "\u9fff") for ch in cleaned)


def _matches_known_person(norm_query: str, kp: KnownPersonRef) -> bool:
    if not norm_query:
        return False
    terms = [kp.display_name, *kp.aliases]
    for raw_term in terms:
        norm_term = normalize_alias_for_index(raw_term)
        if not norm_term:
            continue
        if norm_term == norm_query:
            return True
        if len(norm_term) >= 2 and norm_term in norm_query:
            return True
        if len(norm_query) >= 2 and norm_query in norm_term:
            return True
    return False


def _clause_mentions_known_person(norm_clause: str, kp: KnownPersonRef) -> bool:
    if not norm_clause:
        return False
    for raw_term in [kp.display_name, *kp.aliases]:
        norm_term = normalize_alias_for_index(raw_term)
        if len(norm_term) >= 2 and norm_term in norm_clause:
            return True
    return False


@dataclass(frozen=True, slots=True)
class ProvisionedPerson:
    """Summary of a newly auto-provisioned `provisional` `crystal` person."""

    id: UUID
    name_cn: str
    monogram: str
    role_tag: str
    tier: Tier
    state: PersonState
    rank: str

    @property
    def person_id(self) -> UUID:
        return self.id

    @property
    def display_name(self) -> str:
        return self.name_cn


@dataclass(frozen=True, slots=True)
class EntityResolutionResult:
    """Outcome of resolving a person mention within a capture clause."""

    person_id: UUID | None
    candidate_person_ids: list[UUID] = field(default_factory=list)
    ambiguous: bool = False
    is_new_person: bool = False
    scoped_locked: bool = False
    person_state: PersonState | None = None
    tier: Tier | None = None
    display_name: str | None = None
    matched_via: str = "unresolved"
    confidence: float = 0.0

    @property
    def candidate_count(self) -> int:
        return len(self.candidate_person_ids)


class EntityResolver:
    """Reusable application service for deterministic-first person entity resolution."""

    def __init__(
        self,
        cipher_or_container: EnvelopeCipher | AppContainer | None = None,
        *,
        clock: Clock | None = None,
    ) -> None:
        if cipher_or_container is None:
            self._cipher = EnvelopeCipher(AliyunKmsStubProvider(key_id="stateless"))
            self._clock: Clock = clock or SystemClock()
        elif isinstance(cipher_or_container, EnvelopeCipher):
            self._cipher = cipher_or_container
            self._clock = clock or SystemClock()
        else:
            self._cipher = cipher_or_container.cipher
            self._clock = clock or cipher_or_container.clock
        self._repo = PeopleRepository(self._cipher)

    async def list_known_persons(
        self,
        session: AsyncSession,
        scope: UserScope,
        dek: bytes,
    ) -> list[KnownPersonRef]:
        """Decrypt all non-deleted, non-archived persons + aliases for `scope`."""
        stmt = select(PersonRow).where(
            PersonRow.user_id == scope.user_id,
            PersonRow.deleted_at.is_(None),
            PersonRow.archived_at.is_(None),
            PersonRow.state != PersonState.ARCHIVED.value,
        )
        rows = list((await session.scalars(stmt)).all())
        if not rows:
            return []

        rank_entries = [self._repo.to_rank_entry(r) for r in rows]
        partition = partition_person_ids(rank_entries, include_provisional=True)
        ordered_ids = [*partition.core_ids, *partition.casual_ids]
        row_by_id = {r.id: r for r in rows}

        alias_rows_by_pid = await self._repo.list_aliases_for_persons(session, scope, ordered_ids)

        result: list[KnownPersonRef] = []
        for pid in ordered_ids:
            row = row_by_id.get(pid)
            if row is None:
                continue
            profile = self._repo.decrypt_person_row(row, scope=scope, dek=dek)
            seen_norms: set[str] = {normalize_alias_for_index(profile.name_cn)}
            aliases: list[str] = [profile.name_cn]
            for a_row in alias_rows_by_pid.get(pid, ()):
                decrypted_alias = self._repo.decrypt_alias_row(a_row, scope=scope, dek=dek)
                norm = normalize_alias_for_index(decrypted_alias.alias)
                if norm and norm not in seen_norms:
                    seen_norms.add(norm)
                    aliases.append(decrypted_alias.alias)
            result.append(
                KnownPersonRef(
                    person_id=profile.id,
                    display_name=profile.name_cn,
                    aliases=aliases,
                    tier=profile.tier,
                    role_tag=profile.role_tag,
                )
            )
        return result

    async def create_provisional_person(
        self,
        session: AsyncSession,
        scope: UserScope,
        dek: bytes,
        *,
        name_cn: str | None = None,
        display_name: str | None = None,
        role_tag: str = "",
    ) -> ProvisionedPerson:
        """Provision a `provisional` `crystal` person with primary blind-indexed alias."""
        raw_name = (name_cn if name_cn is not None else display_name) or ""
        cleaned_name = raw_name.strip()[:MAX_NAME_CN_LEN]
        if not cleaned_name:
            raise ValueError("Cannot provision a provisional person with an empty name.")
        cleaned_role = validate_optional_copy_field(
            role_tag, field_name="roleTag", max_length=MAX_ROLE_TAG_LEN
        )
        monogram = derive_monogram(cleaned_name)
        now = self._clock.now()

        locked_rows = await self._repo.lock_user_persons_for_update(session, scope)
        entries = [self._repo.to_rank_entry(r) for r in locked_rows]
        new_rank, peer_updates = compute_new_person_rank(entries, tier=Tier.CRYSTAL)
        if peer_updates:
            row_map = {r.id: r for r in locked_rows}
            for pid, updated_rank in peer_updates.items():
                if pid in row_map:
                    row_map[pid].rank = updated_rank
                    row_map[pid].updated_at = now

        person_id = new_id()
        await self._repo.create_person_with_aliases(
            session,
            scope,
            dek,
            person_id=person_id,
            slug=None,
            name_cn=cleaned_name,
            monogram=monogram,
            role_tag=cleaned_role,
            context_sub="",
            short_memo="",
            traits=[],
            tier=Tier.CRYSTAL,
            rank=new_rank,
            state=PersonState.PROVISIONAL,
            visual_tokens=default_visual_tokens(Tier.CRYSTAL),
            extra_aliases=(),
            now=now,
        )
        return ProvisionedPerson(
            id=person_id,
            name_cn=cleaned_name,
            monogram=monogram,
            role_tag=cleaned_role,
            tier=Tier.CRYSTAL,
            state=PersonState.PROVISIONAL,
            rank=new_rank,
        )

    async def _load_state_map(
        self,
        session: AsyncSession,
        scope: UserScope,
        person_ids: Sequence[UUID],
    ) -> dict[UUID, PersonState]:
        if not person_ids:
            return {}
        stmt = select(PersonRow.id, PersonRow.state).where(
            PersonRow.user_id == scope.user_id,
            PersonRow.id.in_(person_ids),
            PersonRow.deleted_at.is_(None),
        )
        rows = (await session.execute(stmt)).all()
        return {pid: PersonState(st) for pid, st in rows}

    async def resolve(
        self,
        session: AsyncSession,
        scope: UserScope,
        dek: bytes,
        *,
        mention: str | None = None,
        clause_text: str | None = None,
        route_mode: RouteMode | str = RouteMode.GLOBAL,
        scoped_person_id: UUID | None = None,
        llm_matched_person_id: UUID | None = None,
        llm_candidate_person_ids: Sequence[UUID] = (),
        known_persons: list[KnownPersonRef] | None = None,
        allow_provisional_creation: bool = True,
    ) -> EntityResolutionResult:
        """Resolve a person mention according to the 4-tier deterministic priority cascade."""
        mode = route_mode if isinstance(route_mode, RouteMode) else RouteMode(route_mode)
        cleaned_mention = (mention or "").strip()
        norm_mention = normalize_alias_for_index(cleaned_mention)
        norm_clause = normalize_alias_for_index(clause_text or "")

        known = (
            known_persons
            if known_persons is not None
            else await self.list_known_persons(session, scope, dek)
        )
        known_by_id = {kp.person_id: kp for kp in known}
        state_map = await self._load_state_map(session, scope, list(known_by_id.keys()))

        # ------------------------------------------------------------------
        # Priority 1: route_mode == CURRENT with scoped_person_id hard-lock
        # ------------------------------------------------------------------
        if mode is RouteMode.CURRENT and scoped_person_id is not None:
            scoped_kp = known_by_id.get(scoped_person_id)
            if scoped_kp is not None:
                # Check if clause/mention explicitly names a DIFFERENT known person
                if cleaned_mention and not _is_generic_pronoun(cleaned_mention):
                    if not _matches_known_person(norm_mention, scoped_kp):
                        hmac_val = self._repo.compute_alias_hmac(
                            dek, scope=scope, alias=cleaned_mention
                        )
                        blind_rows = await self._repo.lookup_persons_by_alias_hmac(
                            session, scope, hmac_val
                        )
                        if blind_rows and all(r.id != scoped_person_id for r in blind_rows):
                            if len(blind_rows) == 1:
                                hit = blind_rows[0]
                                hit_name = self._repo.decrypt_person_str(
                                    dek,
                                    hit.display_name_enc,
                                    scope=scope,
                                    person_id=hit.id,
                                    column="display_name_enc",
                                )
                                return EntityResolutionResult(
                                    person_id=hit.id,
                                    candidate_person_ids=[hit.id],
                                    ambiguous=False,
                                    is_new_person=False,
                                    scoped_locked=False,
                                    person_state=PersonState(hit.state),
                                    tier=Tier(hit.tier),
                                    display_name=hit_name,
                                    matched_via="blind_index",
                                    confidence=1.0,
                                )
                            return EntityResolutionResult(
                                person_id=None,
                                candidate_person_ids=[r.id for r in blind_rows],
                                ambiguous=True,
                                is_new_person=False,
                                scoped_locked=False,
                                matched_via="blind_index",
                                confidence=0.5,
                            )

                        other_sub = [
                            kp
                            for kp in known
                            if kp.person_id != scoped_person_id
                            and _matches_known_person(norm_mention, kp)
                        ]
                        if len(other_sub) == 1:
                            hit_kp = other_sub[0]
                            return EntityResolutionResult(
                                person_id=hit_kp.person_id,
                                candidate_person_ids=[hit_kp.person_id],
                                ambiguous=False,
                                is_new_person=False,
                                scoped_locked=False,
                                person_state=state_map.get(hit_kp.person_id, PersonState.ACTIVE),
                                tier=hit_kp.tier,
                                display_name=hit_kp.display_name,
                                matched_via="known_substring",
                                confidence=0.9,
                            )
                        if len(other_sub) > 1:
                            return EntityResolutionResult(
                                person_id=None,
                                candidate_person_ids=[kp.person_id for kp in other_sub],
                                ambiguous=True,
                                is_new_person=False,
                                scoped_locked=False,
                                matched_via="known_substring",
                                confidence=0.5,
                            )

                if norm_clause and not _clause_mentions_known_person(norm_clause, scoped_kp):
                    clause_others = [
                        kp
                        for kp in known
                        if kp.person_id != scoped_person_id
                        and _clause_mentions_known_person(norm_clause, kp)
                    ]
                    if len(clause_others) == 1:
                        hit_kp = clause_others[0]
                        return EntityResolutionResult(
                            person_id=hit_kp.person_id,
                            candidate_person_ids=[hit_kp.person_id],
                            ambiguous=False,
                            is_new_person=False,
                            scoped_locked=False,
                            person_state=state_map.get(hit_kp.person_id, PersonState.ACTIVE),
                            tier=hit_kp.tier,
                            display_name=hit_kp.display_name,
                            matched_via="known_substring",
                            confidence=0.9,
                        )
                    if len(clause_others) > 1:
                        return EntityResolutionResult(
                            person_id=None,
                            candidate_person_ids=[kp.person_id for kp in clause_others],
                            ambiguous=True,
                            is_new_person=False,
                            scoped_locked=False,
                            matched_via="known_substring",
                            confidence=0.5,
                        )

                # Hard-lock scoped_person_id!
                return EntityResolutionResult(
                    person_id=scoped_person_id,
                    candidate_person_ids=[scoped_person_id],
                    ambiguous=False,
                    is_new_person=False,
                    scoped_locked=True,
                    person_state=state_map.get(scoped_person_id, PersonState.ACTIVE),
                    tier=scoped_kp.tier,
                    display_name=scoped_kp.display_name,
                    matched_via="scoped_lock",
                    confidence=1.0,
                )

        # ------------------------------------------------------------------
        # Priority 2: Exact blind-index lookup on `person_aliases` (`alias_hmac`)
        # ------------------------------------------------------------------
        if cleaned_mention and not _is_generic_pronoun(cleaned_mention):
            hmac_val = self._repo.compute_alias_hmac(dek, scope=scope, alias=cleaned_mention)
            blind_matches = await self._repo.lookup_persons_by_alias_hmac(session, scope, hmac_val)
            if len(blind_matches) == 1:
                hit = blind_matches[0]
                hit_name = self._repo.decrypt_person_str(
                    dek,
                    hit.display_name_enc,
                    scope=scope,
                    person_id=hit.id,
                    column="display_name_enc",
                )
                return EntityResolutionResult(
                    person_id=hit.id,
                    candidate_person_ids=[hit.id],
                    ambiguous=False,
                    is_new_person=False,
                    scoped_locked=False,
                    person_state=PersonState(hit.state),
                    tier=Tier(hit.tier),
                    display_name=hit_name,
                    matched_via="blind_index",
                    confidence=1.0,
                )
            if len(blind_matches) > 1:
                return EntityResolutionResult(
                    person_id=None,
                    candidate_person_ids=[r.id for r in blind_matches],
                    ambiguous=True,
                    is_new_person=False,
                    scoped_locked=False,
                    matched_via="blind_index",
                    confidence=0.5,
                )

        # ------------------------------------------------------------------
        # Priority 3: Substring / mention match against decrypted KnownPersonRef list
        # ------------------------------------------------------------------
        if cleaned_mention and not _is_generic_pronoun(cleaned_mention):
            sub_matches = [kp for kp in known if _matches_known_person(norm_mention, kp)]
            if len(sub_matches) == 1:
                hit_kp = sub_matches[0]
                return EntityResolutionResult(
                    person_id=hit_kp.person_id,
                    candidate_person_ids=[hit_kp.person_id],
                    ambiguous=False,
                    is_new_person=False,
                    scoped_locked=False,
                    person_state=state_map.get(hit_kp.person_id, PersonState.ACTIVE),
                    tier=hit_kp.tier,
                    display_name=hit_kp.display_name,
                    matched_via="known_substring",
                    confidence=0.9,
                )
            if len(sub_matches) > 1:
                return EntityResolutionResult(
                    person_id=None,
                    candidate_person_ids=[kp.person_id for kp in sub_matches],
                    ambiguous=True,
                    is_new_person=False,
                    scoped_locked=False,
                    matched_via="known_substring",
                    confidence=0.5,
                )

        if norm_clause:
            clause_matches = [kp for kp in known if _clause_mentions_known_person(norm_clause, kp)]
            if len(clause_matches) == 1:
                hit_kp = clause_matches[0]
                return EntityResolutionResult(
                    person_id=hit_kp.person_id,
                    candidate_person_ids=[hit_kp.person_id],
                    ambiguous=False,
                    is_new_person=False,
                    scoped_locked=False,
                    person_state=state_map.get(hit_kp.person_id, PersonState.ACTIVE),
                    tier=hit_kp.tier,
                    display_name=hit_kp.display_name,
                    matched_via="known_substring",
                    confidence=0.85,
                )
            if len(clause_matches) > 1:
                return EntityResolutionResult(
                    person_id=None,
                    candidate_person_ids=[kp.person_id for kp in clause_matches],
                    ambiguous=True,
                    is_new_person=False,
                    scoped_locked=False,
                    matched_via="known_substring",
                    confidence=0.5,
                )

        valid_llm_candidates = [cid for cid in llm_candidate_person_ids if cid in known_by_id]
        if len(valid_llm_candidates) > 1:
            return EntityResolutionResult(
                person_id=None,
                candidate_person_ids=valid_llm_candidates,
                ambiguous=True,
                is_new_person=False,
                scoped_locked=False,
                matched_via="llm_candidate",
                confidence=0.5,
            )
        if llm_matched_person_id is not None and llm_matched_person_id in known_by_id:
            hit_kp = known_by_id[llm_matched_person_id]
            return EntityResolutionResult(
                person_id=hit_kp.person_id,
                candidate_person_ids=[hit_kp.person_id],
                ambiguous=False,
                is_new_person=False,
                scoped_locked=False,
                person_state=state_map.get(hit_kp.person_id, PersonState.ACTIVE),
                tier=hit_kp.tier,
                display_name=hit_kp.display_name,
                matched_via="llm_candidate",
                confidence=0.85,
            )
        if len(valid_llm_candidates) == 1:
            hit_kp = known_by_id[valid_llm_candidates[0]]
            return EntityResolutionResult(
                person_id=hit_kp.person_id,
                candidate_person_ids=[hit_kp.person_id],
                ambiguous=False,
                is_new_person=False,
                scoped_locked=False,
                person_state=state_map.get(hit_kp.person_id, PersonState.ACTIVE),
                tier=hit_kp.tier,
                display_name=hit_kp.display_name,
                matched_via="llm_candidate",
                confidence=0.85,
            )

        # ------------------------------------------------------------------
        # Priority 4: Auto-provision `provisional` `crystal` person in GLOBAL mode
        # ------------------------------------------------------------------
        if (
            mode is RouteMode.GLOBAL
            and allow_provisional_creation
            and _is_plausible_new_person_name(cleaned_mention)
        ):
            provisioned = await self.create_provisional_person(
                session,
                scope,
                dek,
                name_cn=cleaned_mention,
            )
            if known_persons is not None:
                known_persons.append(
                    KnownPersonRef(
                        person_id=provisioned.id,
                        display_name=provisioned.name_cn,
                        aliases=[provisioned.name_cn],
                        tier=Tier.CRYSTAL,
                        role_tag=provisioned.role_tag,
                    )
                )
            return EntityResolutionResult(
                person_id=provisioned.id,
                candidate_person_ids=[provisioned.id],
                ambiguous=False,
                is_new_person=True,
                scoped_locked=False,
                person_state=PersonState.PROVISIONAL,
                tier=Tier.CRYSTAL,
                display_name=provisioned.name_cn,
                matched_via="provisional_created",
                confidence=0.75,
            )

        return EntityResolutionResult(
            person_id=None,
            candidate_person_ids=[],
            ambiguous=False,
            is_new_person=False,
            scoped_locked=False,
            matched_via="unresolved",
            confidence=0.0,
        )

    # Convenience alias
    resolve_mention = resolve


async def list_known_persons(
    session: AsyncSession,
    scope: UserScope,
    dek: bytes,
    *,
    cipher: EnvelopeCipher | None = None,
) -> list[KnownPersonRef]:
    """Functional helper delegating to `EntityResolver.list_known_persons`."""
    return await EntityResolver(cipher).list_known_persons(session, scope, dek)


async def create_provisional_person(
    session: AsyncSession,
    scope: UserScope,
    dek: bytes,
    *,
    name_cn: str,
    cipher: EnvelopeCipher | None = None,
    clock: Clock | None = None,
    role_tag: str = "",
) -> ProvisionedPerson:
    """Functional helper delegating to `EntityResolver.create_provisional_person`."""
    return await EntityResolver(cipher, clock=clock).create_provisional_person(
        session, scope, dek, name_cn=name_cn, role_tag=role_tag
    )
