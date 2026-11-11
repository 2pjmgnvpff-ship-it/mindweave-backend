"""Unit tests for People pure domain policies: 3-State Arrow Tiers, LexoRank Move, and UI Copy Sanitizer."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

import pytest

from mindweave.core.dto import FORBIDDEN_UI_DEV_PHRASES as CORE_FORBIDDEN_PHRASES
from mindweave.core.ids import new_id
from mindweave.modules.bootstrap.domain import (
    format_duration_ms,
    sanitize_material_preview_text,
)
from mindweave.modules.people.domain import (
    FORBIDDEN_UI_DEV_PHRASES,
    CopyPolicyViolationError,
    InvalidMoveError,
    InvalidTierTransitionError,
    PersonRankEntry,
    compute_move,
    compute_new_person_rank,
    compute_tier_transition,
    default_visual_tokens,
    derive_cupid_title,
    derive_monogram,
    partition_person_ids,
    validate_alias_text,
    validate_name_cn,
    validate_optional_copy_field,
    validate_traits,
    validate_visual_tokens,
)
from mindweave.shared.enums import PersonState, Tier
from mindweave.shared.lexorank import REBALANCE_MAX_LEN, initial_ranks, validate_rank


def _make_entries(
    specs: list[tuple[Tier, PersonState, bool]],
) -> list[PersonRankEntry]:
    ranks = initial_ranks(len(specs))
    return [
        PersonRankEntry(
            person_id=new_id(),
            tier=tier,
            rank=rank,
            state=state,
            deleted=deleted,
        )
        for (tier, state, deleted), rank in zip(specs, ranks, strict=True)
    ]


def _apply_plan_ranks(
    entries: list[PersonRankEntry],
    *,
    target_id: UUID,
    new_tier: Tier,
    updates: Mapping[UUID, str],
) -> list[PersonRankEntry]:
    updated: list[PersonRankEntry] = []
    for entry in entries:
        tier = new_tier if entry.person_id == target_id else entry.tier
        rank = updates.get(entry.person_id, entry.rank)
        updated.append(
            PersonRankEntry(
                person_id=entry.person_id,
                tier=tier,
                rank=rank,
                state=entry.state,
                deleted=entry.deleted,
            )
        )
    return updated


def test_demote_core_to_crystal_prepends_to_front_of_casual_ids() -> None:
    core_ranks = initial_ranks(3)
    casual_ranks = initial_ranks(6)

    chloe, xubai, zhixia = new_id(), new_id(), new_id()
    casual_people = [new_id() for _ in range(6)]

    entries = [
        PersonRankEntry(chloe, Tier.GOLD, core_ranks[0]),
        PersonRankEntry(xubai, Tier.GOLD, core_ranks[1]),
        PersonRankEntry(zhixia, Tier.SILVER, core_ranks[2]),
        *[
            PersonRankEntry(pid, Tier.CRYSTAL, r)
            for pid, r in zip(casual_people, casual_ranks, strict=True)
        ],
    ]

    # Gold -> Crystal prepends Chloe to casualIds[0]
    plan1 = compute_tier_transition(entries, person_id=chloe, target_tier=Tier.CRYSTAL)
    assert plan1.previous_tier is Tier.GOLD
    assert plan1.new_tier is Tier.CRYSTAL
    assert plan1.core_ids == [xubai, zhixia]
    assert plan1.casual_ids == [chloe, *casual_people]
    assert validate_rank(plan1.new_rank) < casual_ranks[0]

    entries = _apply_plan_ranks(
        entries, target_id=chloe, new_tier=Tier.CRYSTAL, updates=plan1.rank_updates
    )

    # Silver -> Crystal prepends Zhixia ahead of Chloe at casualIds[0]
    plan2 = compute_tier_transition(entries, person_id=zhixia, target_tier=Tier.CRYSTAL)
    assert plan2.previous_tier is Tier.SILVER
    assert plan2.new_tier is Tier.CRYSTAL
    assert plan2.core_ids == [xubai]
    assert plan2.casual_ids == [zhixia, chloe, *casual_people]

    # Demoting into an initially empty casualIds also works cleanly
    solo_core = [PersonRankEntry(xubai, Tier.GOLD, "i")]
    plan_empty_casual = compute_tier_transition(
        solo_core, person_id=xubai, target_tier=Tier.CRYSTAL
    )
    assert plan_empty_casual.core_ids == []
    assert plan_empty_casual.casual_ids == [xubai]


def test_promote_crystal_to_core_appends_to_end_of_core_ids() -> None:
    core_ranks = initial_ranks(2)
    casual_ranks = initial_ranks(3)
    c1, c2 = new_id(), new_id()
    p1, p2, p3 = new_id(), new_id(), new_id()

    entries = [
        PersonRankEntry(c1, Tier.GOLD, core_ranks[0]),
        PersonRankEntry(c2, Tier.SILVER, core_ranks[1]),
        PersonRankEntry(p1, Tier.CRYSTAL, casual_ranks[0]),
        PersonRankEntry(p2, Tier.CRYSTAL, casual_ranks[1]),
        PersonRankEntry(p3, Tier.CRYSTAL, casual_ranks[2]),
    ]

    # Crystal -> Gold appends p2 to end of coreIds
    plan1 = compute_tier_transition(entries, person_id=p2, target_tier=Tier.GOLD)
    assert plan1.previous_tier is Tier.CRYSTAL
    assert plan1.new_tier is Tier.GOLD
    assert plan1.core_ids == [c1, c2, p2]
    assert plan1.casual_ids == [p1, p3]
    assert validate_rank(plan1.new_rank) > core_ranks[1]

    entries = _apply_plan_ranks(
        entries, target_id=p2, new_tier=Tier.GOLD, updates=plan1.rank_updates
    )

    # Crystal -> Silver appends p1 after p2 at end of coreIds
    plan2 = compute_tier_transition(entries, person_id=p1, target_tier=Tier.SILVER)
    assert plan2.core_ids == [c1, c2, p2, p1]
    assert plan2.casual_ids == [p3]

    # Promoting into an initially empty coreIds works cleanly
    solo_casual = [PersonRankEntry(p3, Tier.CRYSTAL, "i")]
    plan_empty_core = compute_tier_transition(solo_casual, person_id=p3, target_tier=Tier.GOLD)
    assert plan_empty_core.core_ids == [p3]
    assert plan_empty_core.casual_ids == []


def test_gold_silver_switch_preserves_exact_position_in_core_ids() -> None:
    ranks = initial_ranks(4)
    p0, p1, p2, p3 = new_id(), new_id(), new_id(), new_id()
    entries = [
        PersonRankEntry(p0, Tier.GOLD, ranks[0]),
        PersonRankEntry(p1, Tier.GOLD, ranks[1]),
        PersonRankEntry(p2, Tier.SILVER, ranks[2]),
        PersonRankEntry(p3, Tier.GOLD, ranks[3]),
    ]

    # Switch middle item p1: gold -> silver keeps index 1
    plan_to_silver = compute_tier_transition(entries, person_id=p1, target_tier=Tier.SILVER)
    assert plan_to_silver.previous_tier is Tier.GOLD
    assert plan_to_silver.new_tier is Tier.SILVER
    assert plan_to_silver.core_ids == [p0, p1, p2, p3]
    assert plan_to_silver.new_rank == ranks[1]

    entries = _apply_plan_ranks(
        entries, target_id=p1, new_tier=Tier.SILVER, updates=plan_to_silver.rank_updates
    )

    # Switch p2: silver -> gold keeps index 2
    plan_to_gold = compute_tier_transition(entries, person_id=p2, target_tier=Tier.GOLD)
    assert plan_to_gold.previous_tier is Tier.SILVER
    assert plan_to_gold.new_tier is Tier.GOLD
    assert plan_to_gold.core_ids == [p0, p1, p2, p3]
    assert plan_to_gold.new_rank == ranks[2]

    # Same-tier no-op preserves everything
    noop = compute_tier_transition(entries, person_id=p0, target_tier=Tier.GOLD)
    assert noop.rank_updates == {}
    assert noop.core_ids == [p0, p1, p2, p3]


def test_auto_rebalance_triggers_when_ranks_exceed_max_length() -> None:
    # Force an overlong rank (> REBALANCE_MAX_LEN) in casual bucket, then demote a core person
    long_rank = "0" * REBALANCE_MAX_LEN + "i"
    core_id = new_id()
    casual_a, casual_b = new_id(), new_id()
    entries = [
        PersonRankEntry(core_id, Tier.GOLD, "i"),
        PersonRankEntry(casual_a, Tier.CRYSTAL, long_rank),
        PersonRankEntry(casual_b, Tier.CRYSTAL, "m"),
    ]

    plan = compute_tier_transition(entries, person_id=core_id, target_tier=Tier.CRYSTAL)
    assert plan.rebalanced is True
    assert plan.casual_ids == [core_id, casual_a, casual_b]
    for pid in plan.casual_ids:
        r = plan.rank_updates[pid]
        assert len(validate_rank(r)) <= REBALANCE_MAX_LEN

    rebalanced_ranks = [plan.rank_updates[pid] for pid in plan.casual_ids]
    assert rebalanced_ranks == sorted(rebalanced_ranks)


def test_partition_filters_archived_deleted_and_provisional() -> None:
    entries = _make_entries(
        [
            (Tier.GOLD, PersonState.ACTIVE, False),
            (Tier.SILVER, PersonState.ARCHIVED, False),
            (Tier.GOLD, PersonState.ACTIVE, True),
            (Tier.CRYSTAL, PersonState.ACTIVE, False),
            (Tier.CRYSTAL, PersonState.PROVISIONAL, False),
        ]
    )

    with_prov = partition_person_ids(entries, include_provisional=True)
    assert with_prov.core_ids == [entries[0].person_id]
    assert with_prov.casual_ids == [entries[3].person_id, entries[4].person_id]

    without_prov = partition_person_ids(entries, include_provisional=False)
    assert without_prov.core_ids == [entries[0].person_id]
    assert without_prov.casual_ids == [entries[3].person_id]

    with pytest.raises(InvalidTierTransitionError):
        compute_tier_transition(entries, person_id=entries[1].person_id, target_tier=Tier.CRYSTAL)
    with pytest.raises(InvalidTierTransitionError):
        compute_tier_transition(entries, person_id=new_id(), target_tier=Tier.GOLD)


def test_move_reorders_within_bucket_and_validates_anchors() -> None:
    ranks = initial_ranks(4)
    a, b, c, d = new_id(), new_id(), new_id(), new_id()
    casual_x = new_id()
    entries = [
        PersonRankEntry(a, Tier.GOLD, ranks[0]),
        PersonRankEntry(b, Tier.SILVER, ranks[1]),
        PersonRankEntry(c, Tier.GOLD, ranks[2]),
        PersonRankEntry(d, Tier.GOLD, ranks[3]),
        PersonRankEntry(casual_x, Tier.CRYSTAL, "i"),
    ]

    # Move d to very front before a
    m1 = compute_move(entries, person_id=d, before_person_id=a)
    assert m1.core_ids == [d, a, b, c]
    assert m1.casual_ids == [casual_x]

    entries = _apply_plan_ranks(entries, target_id=d, new_tier=Tier.GOLD, updates=m1.rank_updates)

    # Move d between a and b using explicit after + before
    m2 = compute_move(entries, person_id=d, after_person_id=a, before_person_id=b)
    assert m2.core_ids == [a, d, b, c]

    entries = _apply_plan_ranks(entries, target_id=d, new_tier=Tier.GOLD, updates=m2.rank_updates)

    # Move a to very end after c
    m3 = compute_move(entries, person_id=a, after_person_id=c)
    assert m3.core_ids == [d, b, c, a]

    # Invalid move cases
    with pytest.raises(InvalidMoveError):
        compute_move(entries, person_id=a)
    with pytest.raises(InvalidMoveError):
        compute_move(entries, person_id=a, after_person_id=a)
    with pytest.raises(InvalidMoveError):
        compute_move(entries, person_id=a, before_person_id=casual_x)
    with pytest.raises(InvalidMoveError):
        compute_move(entries, person_id=a, after_person_id=d, before_person_id=c)

    # New person tail rank calculation
    tail_rank, peer_updates = compute_new_person_rank(entries, tier=Tier.GOLD)
    assert validate_rank(tail_rank)
    assert peer_updates == {}


def test_copy_policy_monogram_cupid_title_and_bootstrap_formatting() -> None:
    assert set(CORE_FORBIDDEN_PHRASES).issubset(set(FORBIDDEN_UI_DEV_PHRASES))

    assert derive_monogram("沈清秋") == "沈"
    assert derive_monogram("陈教授") == "陈"
    assert derive_monogram("kevin chen") == "K"
    assert derive_monogram("·苏敏姐") == "苏"
    assert derive_monogram("老顾", "顾") == "顾"
    assert derive_cupid_title("沈清秋") == "沈清秋的丘比特"
    assert "sealBg" in default_visual_tokens(Tier.CRYSTAL)

    assert validate_name_cn(" 沈清秋 ") == "沈清秋"
    assert (
        validate_optional_copy_field(" 心动伴侣 ", field_name="roleTag", max_length=64)
        == "心动伴侣"
    )
    assert validate_traits([" 偏爱桂花燕麦拿铁 ", "", "忌冰美式"]) == [
        "偏爱桂花燕麦拿铁",
        "忌冰美式",
    ]
    assert validate_alias_text(" 清秋 ") == "清秋"
    assert validate_visual_tokens({"tabColor": "#D49A45"}) == {"tabColor": "#D49A45"}

    for bad_phrase in (
        "系统自动归档",
        "AI 提炼卡片",
        "温情伴读模式",
        "移出胶囊归入名录",
        "[rec_01]",
    ):
        with pytest.raises(CopyPolicyViolationError):
            validate_optional_copy_field(
                f"备注：{bad_phrase}", field_name="shortMemo", max_length=240
            )
        with pytest.raises(CopyPolicyViolationError):
            validate_traits([f"偏好 · {bad_phrase}"])

    assert format_duration_ms(None) is None
    assert format_duration_ms(0) == "0:00"
    assert format_duration_ms(4_000) == "0:04"
    assert format_duration_ms(18_000) == "0:18"
    assert format_duration_ms(75_000) == "1:15"
    assert sanitize_material_preview_text("系统自动归档 · 海边看海鸥约定") == "海边看海鸥约定"
