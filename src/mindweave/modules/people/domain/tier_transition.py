"""Pure 3-State Arrow Tier transition and LexoRank ordering policies (Invariant I7).

Rules (V10 Spec §一.1 & BACKEND_MASTER_PLAN §1.3 I7):
- `coreIds` is the ordered list of active non-deleted, non-archived persons where
  `tier in (Tier.GOLD, Tier.SILVER)`, sorted by `rank ASC, id ASC`.
- `casualIds` is the ordered list of active non-deleted, non-archived persons where
  `tier == Tier.CRYSTAL`, sorted by `rank ASC, id ASC`.
- Switching `gold | silver` -> `crystal`: atomically moves out of `coreIds` and
  prepends to the FRONT of `casualIds` (`rank_before(first_casual_rank)`).
- Switching `crystal` -> `gold | silver`: atomically moves out of `casualIds` and
  appends to the END of `coreIds` (`rank_after(last_core_rank)`).
- Switching `gold <-> silver`: keeps the person's current position inside `coreIds`.
- Rebalances ranks via `initial_ranks(len(...))` whenever `needs_rebalance` is True
  or duplicate ranks exist in the target bucket.
- Never deletes or mutates any records, facts, reminders, or chat messages.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from mindweave.modules.people.domain.entities import (
    InvalidMoveError,
    InvalidTierTransitionError,
)
from mindweave.shared.enums import PersonState, Tier
from mindweave.shared.lexorank import (
    initial_ranks,
    needs_rebalance,
    rank_after,
    rank_before,
    rank_between,
)


@dataclass(frozen=True, slots=True)
class PersonRankEntry:
    """Lightweight rank projection of a person for pure ordering calculations."""

    person_id: UUID
    tier: Tier
    rank: str
    state: PersonState = PersonState.ACTIVE
    deleted: bool = False

    def is_in_workspace(self, *, include_provisional: bool = True) -> bool:
        if self.deleted or self.state is PersonState.ARCHIVED:
            return False
        if self.state is PersonState.PROVISIONAL and not include_provisional:
            return False
        return True


@dataclass(frozen=True, slots=True)
class TierPartition:
    """Ordered `coreIds` and `casualIds` lists."""

    core_ids: list[UUID]
    casual_ids: list[UUID]


@dataclass(frozen=True, slots=True)
class TierTransitionPlan:
    """Deterministic result of a 3-state arrow tier transition."""

    person_id: UUID
    previous_tier: Tier
    new_tier: Tier
    new_rank: str
    rank_updates: dict[UUID, str]
    core_ids: list[UUID]
    casual_ids: list[UUID]
    rebalanced: bool = False


@dataclass(frozen=True, slots=True)
class MovePlan:
    """Deterministic result of reordering a person within `coreIds` or `casualIds`."""

    person_id: UUID
    tier: Tier
    new_rank: str
    rank_updates: dict[UUID, str]
    core_ids: list[UUID]
    casual_ids: list[UUID]
    rebalanced: bool = False


def _sort_key(entry: PersonRankEntry) -> tuple[str, UUID]:
    return (entry.rank, entry.person_id)


def _split_sorted_buckets(
    entries: Sequence[PersonRankEntry],
    *,
    include_provisional: bool = True,
) -> tuple[list[PersonRankEntry], list[PersonRankEntry]]:
    visible = [e for e in entries if e.is_in_workspace(include_provisional=include_provisional)]
    core = sorted((e for e in visible if e.tier.is_core), key=_sort_key)
    casual = sorted((e for e in visible if not e.tier.is_core), key=_sort_key)
    return core, casual


def _has_strictly_increasing_ranks(ranks: Sequence[str]) -> bool:
    return all(ranks[i - 1] < ranks[i] for i in range(1, len(ranks)))


def _rebalance_if_needed(
    ordered_entries: list[PersonRankEntry],
) -> tuple[list[PersonRankEntry], dict[UUID, str], bool]:
    ranks = [e.rank for e in ordered_entries]
    if not ordered_entries:
        return [], {}, False
    if not needs_rebalance(ranks) and _has_strictly_increasing_ranks(ranks):
        return ordered_entries, {}, False

    fresh = initial_ranks(len(ordered_entries))
    rebalanced_entries: list[PersonRankEntry] = []
    updates: dict[UUID, str] = {}
    for entry, fresh_rank in zip(ordered_entries, fresh, strict=True):
        if entry.rank != fresh_rank:
            updates[entry.person_id] = fresh_rank
        rebalanced_entries.append(
            PersonRankEntry(
                person_id=entry.person_id,
                tier=entry.tier,
                rank=fresh_rank,
                state=entry.state,
                deleted=entry.deleted,
            )
        )
    return rebalanced_entries, updates, True


def partition_person_ids(
    entries: Sequence[PersonRankEntry],
    *,
    include_provisional: bool = True,
) -> TierPartition:
    """Compute ordered `coreIds` and `casualIds` from person entries (`rank ASC, id ASC`)."""
    core, casual = _split_sorted_buckets(entries, include_provisional=include_provisional)
    return TierPartition(
        core_ids=[e.person_id for e in core],
        casual_ids=[e.person_id for e in casual],
    )


def compute_new_person_rank(
    entries: Sequence[PersonRankEntry],
    *,
    tier: Tier,
) -> tuple[str, dict[UUID, str]]:
    """Compute initial tail rank for a newly created or unarchived person in `tier`'s bucket.

    Returns `(new_rank, peer_rank_updates)` where `peer_rank_updates` is non-empty only if
    existing ranks in that bucket required rebalancing.
    """
    core, casual = _split_sorted_buckets(entries, include_provisional=True)
    bucket = core if tier.is_core else casual
    if not bucket:
        return rank_after(None), {}

    candidate_rank = rank_after(bucket[-1].rank)
    trial_ranks = [*(e.rank for e in bucket), candidate_rank]
    if not needs_rebalance(trial_ranks) and _has_strictly_increasing_ranks(trial_ranks):
        return candidate_rank, {}

    fresh = initial_ranks(len(bucket) + 1)
    peer_updates: dict[UUID, str] = {}
    for entry, fresh_rank in zip(bucket, fresh[:-1], strict=True):
        if entry.rank != fresh_rank:
            peer_updates[entry.person_id] = fresh_rank
    return fresh[-1], peer_updates


def compute_tier_transition(
    entries: Sequence[PersonRankEntry],
    *,
    person_id: UUID,
    target_tier: Tier,
) -> TierTransitionPlan:
    """Execute pure 3-state arrow tier transition with O(1) LexoRank update and auto-rebalance."""
    by_id = {e.person_id: e for e in entries if not e.deleted}
    target = by_id.get(person_id)
    if target is None:
        raise InvalidTierTransitionError("Target person does not exist.")
    if target.state is PersonState.ARCHIVED:
        raise InvalidTierTransitionError("Cannot transition tier of an archived person.")

    prev_tier = target.tier
    core, casual = _split_sorted_buckets(entries, include_provisional=True)

    # Case 1: Same tier (idempotent no-op)
    if prev_tier is target_tier:
        return TierTransitionPlan(
            person_id=person_id,
            previous_tier=prev_tier,
            new_tier=target_tier,
            new_rank=target.rank,
            rank_updates={},
            core_ids=[e.person_id for e in core],
            casual_ids=[e.person_id for e in casual],
            rebalanced=False,
        )

    # Case 2: Core <-> Core (gold <-> silver) — keep exact current position inside coreIds!
    if prev_tier.is_core and target_tier.is_core:
        new_core = [
            PersonRankEntry(
                person_id=e.person_id,
                tier=target_tier if e.person_id == person_id else e.tier,
                rank=e.rank,
                state=e.state,
                deleted=e.deleted,
            )
            for e in core
        ]
        new_core, reb_updates, rebalanced = _rebalance_if_needed(new_core)
        final_target = next(e for e in new_core if e.person_id == person_id)
        return TierTransitionPlan(
            person_id=person_id,
            previous_tier=prev_tier,
            new_tier=target_tier,
            new_rank=final_target.rank,
            rank_updates=reb_updates,
            core_ids=[e.person_id for e in new_core],
            casual_ids=[e.person_id for e in casual],
            rebalanced=rebalanced,
        )

    # Case 3: Demote Core (gold | silver) -> Casual (crystal) — prepend to FRONT of casualIds!
    if prev_tier.is_core and target_tier is Tier.CRYSTAL:
        remaining_core = [e for e in core if e.person_id != person_id]
        remaining_casual = [e for e in casual if e.person_id != person_id]
        first_casual_rank = remaining_casual[0].rank if remaining_casual else None
        prepended_rank = rank_before(first_casual_rank)
        moved = PersonRankEntry(
            person_id=person_id,
            tier=Tier.CRYSTAL,
            rank=prepended_rank,
            state=target.state,
            deleted=False,
        )
        new_casual = [moved, *remaining_casual]
        updates: dict[UUID, str] = {person_id: prepended_rank}
        new_casual, reb_updates, rebalanced = _rebalance_if_needed(new_casual)
        updates.update(reb_updates)
        final_target = new_casual[0]
        return TierTransitionPlan(
            person_id=person_id,
            previous_tier=prev_tier,
            new_tier=Tier.CRYSTAL,
            new_rank=final_target.rank,
            rank_updates=updates,
            core_ids=[e.person_id for e in remaining_core],
            casual_ids=[e.person_id for e in new_casual],
            rebalanced=rebalanced,
        )

    # Case 4: Promote Casual (crystal) -> Core (gold | silver) — append to END of coreIds!
    remaining_casual = [e for e in casual if e.person_id != person_id]
    remaining_core = [e for e in core if e.person_id != person_id]
    last_core_rank = remaining_core[-1].rank if remaining_core else None
    appended_rank = rank_after(last_core_rank)
    moved = PersonRankEntry(
        person_id=person_id,
        tier=target_tier,
        rank=appended_rank,
        state=target.state,
        deleted=False,
    )
    new_core = [*remaining_core, moved]
    updates = {person_id: appended_rank}
    new_core, reb_updates, rebalanced = _rebalance_if_needed(new_core)
    updates.update(reb_updates)
    final_target = new_core[-1]
    return TierTransitionPlan(
        person_id=person_id,
        previous_tier=prev_tier,
        new_tier=target_tier,
        new_rank=final_target.rank,
        rank_updates=updates,
        core_ids=[e.person_id for e in new_core],
        casual_ids=[e.person_id for e in remaining_casual],
        rebalanced=rebalanced,
    )


def compute_move(
    entries: Sequence[PersonRankEntry],
    *,
    person_id: UUID,
    after_person_id: UUID | None = None,
    before_person_id: UUID | None = None,
) -> MovePlan:
    """Reorder `person_id` within its current bucket (`coreIds` or `casualIds`) via LexoRank."""
    if after_person_id is None and before_person_id is None:
        raise InvalidMoveError("At least one of afterPersonId or beforePersonId must be provided.")
    if after_person_id == person_id or before_person_id == person_id:
        raise InvalidMoveError("Cannot position a person relative to itself.")
    if after_person_id is not None and after_person_id == before_person_id:
        raise InvalidMoveError("afterPersonId and beforePersonId cannot be the same person.")

    by_id = {e.person_id: e for e in entries if not e.deleted}
    target = by_id.get(person_id)
    if target is None:
        raise InvalidMoveError("Target person does not exist.")
    if target.state is PersonState.ARCHIVED:
        raise InvalidMoveError("Cannot reorder an archived person.")

    core, casual = _split_sorted_buckets(entries, include_provisional=True)
    is_core = target.tier.is_core
    bucket = core if is_core else casual
    other_bucket = casual if is_core else core
    peers = [e for e in bucket if e.person_id != person_id]
    peer_index = {e.person_id: idx for idx, e in enumerate(peers)}

    if after_person_id is not None and after_person_id not in peer_index:
        raise InvalidMoveError("afterPersonId is not an active member of the same tier group.")
    if before_person_id is not None and before_person_id not in peer_index:
        raise InvalidMoveError("beforePersonId is not an active member of the same tier group.")

    if after_person_id is not None and before_person_id is not None:
        idx_after = peer_index[after_person_id]
        idx_before = peer_index[before_person_id]
        if idx_after + 1 != idx_before:
            raise InvalidMoveError(
                "afterPersonId and beforePersonId must be adjacent in the current ordering."
            )
        insert_at = idx_before
    elif before_person_id is not None:
        insert_at = peer_index[before_person_id]
    else:
        assert after_person_id is not None
        insert_at = peer_index[after_person_id] + 1

    left_rank = peers[insert_at - 1].rank if insert_at > 0 else None
    right_rank = peers[insert_at].rank if insert_at < len(peers) else None

    updates: dict[UUID, str] = {}
    rebalanced = False
    if left_rank is not None and right_rank is not None and not (left_rank < right_rank):
        # Peers had a rank tie — construct exact target order and rebalance cleanly
        desired = [
            *peers[:insert_at],
            PersonRankEntry(
                person_id=person_id,
                tier=target.tier,
                rank=target.rank,
                state=target.state,
                deleted=False,
            ),
            *peers[insert_at:],
        ]
        fresh = initial_ranks(len(desired))
        new_bucket: list[PersonRankEntry] = []
        for entry, fresh_rank in zip(desired, fresh, strict=True):
            if entry.rank != fresh_rank:
                updates[entry.person_id] = fresh_rank
            new_bucket.append(
                PersonRankEntry(
                    person_id=entry.person_id,
                    tier=entry.tier,
                    rank=fresh_rank,
                    state=entry.state,
                    deleted=False,
                )
            )
        rebalanced = True
    else:
        computed_rank = rank_between(left_rank, right_rank)
        moved_entry = PersonRankEntry(
            person_id=person_id,
            tier=target.tier,
            rank=computed_rank,
            state=target.state,
            deleted=False,
        )
        new_bucket = [*peers[:insert_at], moved_entry, *peers[insert_at:]]
        if computed_rank != target.rank:
            updates[person_id] = computed_rank
        new_bucket, reb_updates, rebalanced = _rebalance_if_needed(new_bucket)
        updates.update(reb_updates)

    final_target = next(e for e in new_bucket if e.person_id == person_id)
    new_core = new_bucket if is_core else other_bucket
    new_casual = other_bucket if is_core else new_bucket
    return MovePlan(
        person_id=person_id,
        tier=target.tier,
        new_rank=final_target.rank,
        rank_updates=updates,
        core_ids=[e.person_id for e in new_core],
        casual_ids=[e.person_id for e in new_casual],
        rebalanced=rebalanced,
    )
