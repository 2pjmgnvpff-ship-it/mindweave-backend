"""Locale-safe base-36 fractional indexing (LexoRank) for O(1) person ordering."""

from __future__ import annotations

ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
BASE = len(ALPHABET)  # 36 — strictly lowercase alphanumeric, locale-collation safe
MIN_CHAR = ALPHABET[0]
MAX_CHAR = ALPHABET[-1]
MID_CHAR = ALPHABET[BASE // 2]  # 'i'
DEFAULT_STEP = 4
REBALANCE_MAX_LEN = 24


def _idx(ch: str) -> int:
    pos = ALPHABET.find(ch)
    if pos < 0:
        raise ValueError(f"Invalid LexoRank character: {ch!r}")
    return pos


def validate_rank(rank: str) -> str:
    if not rank:
        raise ValueError("LexoRank cannot be empty")
    for ch in rank:
        _idx(ch)
    if rank[-1] == MIN_CHAR:
        raise ValueError(f"LexoRank must not end with minimum char {MIN_CHAR!r}: {rank!r}")
    return rank


def initial_rank() -> str:
    return MID_CHAR


def initial_ranks(count: int) -> list[str]:
    """Generate `count` evenly spaced initial ranks in ascending order."""
    if count <= 0:
        return []
    if count == 1:
        return [MID_CHAR]
    # Use 3-digit base-36 space [1..36^3 - 2] for smooth initial spacing
    space = BASE**3
    step = space // (count + 1)
    ranks: list[str] = []
    for i in range(1, count + 1):
        val = max(1, min(space - 2, i * step))
        d2, rem = divmod(val, BASE * BASE)
        d1, d0 = divmod(rem, BASE)
        s = (ALPHABET[d2] + ALPHABET[d1] + ALPHABET[d0]).rstrip(MIN_CHAR)
        if not s:
            s = ALPHABET[1]
        ranks.append(s)
    # Guarantee strict monotonicity even for large count
    for idx in range(1, len(ranks)):
        if not (ranks[idx - 1] < ranks[idx]):
            ranks[idx] = rank_after(ranks[idx - 1])
    return ranks


def rank_between(a: str | None, b: str | None) -> str:
    """Compute a rank strictly satisfying a < result < b."""
    if a is not None:
        validate_rank(a)
    if b is not None:
        validate_rank(b)
    if a is not None and b is not None and not (a < b):
        raise ValueError(f"Expected a < b, got {a!r} >= {b!r}")

    if a is None and b is None:
        return initial_rank()

    if a is None and b is not None:
        return _before(b)

    if a is not None and b is None:
        return _after(a)

    assert a is not None and b is not None
    return _midpoint(a, b)


def rank_before(first: str | None) -> str:
    return rank_between(None, first)


def rank_after(last: str | None) -> str:
    return rank_between(last, None)


def needs_rebalance(ranks: list[str], max_len: int = REBALANCE_MAX_LEN) -> bool:
    return any(len(r) > max_len for r in ranks)


def _before(b: str) -> str:
    for i, ch in enumerate(b):
        val = _idx(ch)
        if val > 1:
            return b[:i] + ALPHABET[val // 2]
        if val == 1:
            # Prefix of '0's + midpoint of remaining space
            return b[:i] + MIN_CHAR + MID_CHAR
    # All chars were '0' except невозможно since trailing '0' is forbidden
    return MIN_CHAR * len(b) + MID_CHAR


def _after(a: str) -> str:
    for i, ch in enumerate(a):
        val = _idx(ch)
        if val < BASE - 2:
            nxt = val + max(1, min(DEFAULT_STEP, (BASE - 1 - val) // 2))
            return a[:i] + ALPHABET[nxt]
    return a + MID_CHAR


def _midpoint(a: str, b: str) -> str:
    max_len = max(len(a), len(b))
    out: list[str] = []
    for i in range(max_len):
        ca = _idx(a[i]) if i < len(a) else 0
        cb = _idx(b[i]) if i < len(b) else BASE
        if ca == cb:
            out.append(ALPHABET[ca])
            continue
        if cb - ca > 1:
            mid = (ca + cb) // 2
            candidate = "".join(out) + ALPHABET[mid]
            candidate = candidate.rstrip(MIN_CHAR) or ALPHABET[1]
            if a < candidate < b:
                return candidate
        # Difference is 1 at position i: carry into deeper digits relative to `a`'s suffix
        out.append(ALPHABET[ca])
        suffix_a = a[i + 1 :]
        return "".join(out) + _after_in_unit(suffix_a)
    return a + MID_CHAR


def _after_in_unit(suffix_a: str) -> str:
    """Return a non-empty suffix strictly > suffix_a and < infinite 'z...'."""
    for j, ch in enumerate(suffix_a):
        val = _idx(ch)
        if val < BASE - 1:
            mid = (val + BASE) // 2
            if mid > val:
                return suffix_a[:j] + ALPHABET[mid]
            return suffix_a[:j] + ALPHABET[val] + MID_CHAR
    return suffix_a + MID_CHAR
