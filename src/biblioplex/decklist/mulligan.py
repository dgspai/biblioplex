"""Opening-hand sampling for `/decks/mulligan` (D14.10).

Pure Monte-Carlo sim: expand the deck's resolved rows to `qty` copies of
`(name, cmc, is_land)`, draw `hand_size` cards `n_hands` times, aggregate
land count + cheap-card count + keep-rate.

`keep_rate` is the London-mulligan heuristic — a hand with 2..5 lands is
considered keepable. No formal EV, no bottom-decking sim (would require
mana-color balance + play/draw priors that don't fit a pure fn). Agents
that need more nuance run the sim themselves.

Deterministic when the caller passes a seeded `random.Random`; the pure
fn never reads the wall clock.
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass

__all__ = ["HandStats", "sample_hands"]


@dataclass(frozen=True)
class HandStats:
    """Aggregated stats over `n_hands` sampled opening hands.

    All floats round to a stable JSON shape at the router layer — this
    class carries the raw sim values. `hands=0` is a sentinel for
    "couldn't sample" (empty pool or hand_size > pool); every stat is
    then 0.0 / 0 so downstream code needn't branch.
    """

    hands: int
    hand_size: int
    land_min: int
    land_max: int
    land_median: float
    land_mean: float
    cmc_le_3_mean: float
    keep_rate: float


def _zero_stats(hand_size: int) -> HandStats:
    return HandStats(
        hands=0,
        hand_size=hand_size,
        land_min=0,
        land_max=0,
        land_median=0.0,
        land_mean=0.0,
        cmc_le_3_mean=0.0,
        keep_rate=0.0,
    )


def sample_hands(
    pool: list[tuple[str, int | None, bool]],
    *,
    n_hands: int,
    hand_size: int,
    rng: random.Random,
    keep_land_min: int = 2,
    keep_land_max: int = 5,
) -> HandStats:
    """Draw `n_hands` × `hand_size` opening hands and aggregate stats.

    ``pool`` is a flat list of ``(name, cmc, is_land)`` tuples — one entry
    per physical copy (a 4-of contributes 4 tuples). Order is irrelevant;
    ``rng.sample`` is uniform.

    Returns :func:`_zero_stats` when the sim can't run (empty pool,
    non-positive counts, or hand_size exceeds pool size — a 7-card
    opening from a 6-card pool is undefined).

    Ponytail: statistics.median/mean over Python ints is fine at 500
    hands; even 10k is <100 ms. Numpy would be premature.
    """
    if not pool or hand_size <= 0 or n_hands <= 0 or hand_size > len(pool):
        return _zero_stats(hand_size)

    land_counts: list[int] = []
    cheap_counts: list[int] = []
    for _ in range(n_hands):
        hand = rng.sample(pool, hand_size)
        n_lands = 0
        n_cheap = 0
        for _name, cmc, is_land in hand:
            if is_land:
                n_lands += 1
            if cmc is not None and cmc <= 3:
                n_cheap += 1
        land_counts.append(n_lands)
        cheap_counts.append(n_cheap)

    keeps = sum(1 for n in land_counts if keep_land_min <= n <= keep_land_max)
    return HandStats(
        hands=n_hands,
        hand_size=hand_size,
        land_min=min(land_counts),
        land_max=max(land_counts),
        land_median=float(statistics.median(land_counts)),
        land_mean=float(statistics.mean(land_counts)),
        cmc_le_3_mean=float(statistics.mean(cheap_counts)),
        keep_rate=keeps / n_hands,
    )
