"""Pure-fn tests for the mulligan sampler (D14.10)."""

from __future__ import annotations

import random

from biblioplex.decklist.mulligan import HandStats, sample_hands


def _card(name: str, cmc: int | None, is_land: bool) -> tuple[str, int | None, bool]:
    return (name, cmc, is_land)


def _make_pool(n_lands: int, n_spells: int, spell_cmc: int = 3) -> list:
    """Simple pool: n_lands `Forest` + n_spells `Bolt`s at spell_cmc."""
    pool = [_card("Forest", 0, True)] * n_lands
    pool += [_card("Bolt", spell_cmc, False)] * n_spells
    return pool


def test_all_lands_100_percent_keep_when_hand_size_is_all_lands() -> None:
    """40-land deck, 7-card hand → 7 lands per hand → all above the 5-land cap."""
    pool = _make_pool(n_lands=40, n_spells=0)
    stats = sample_hands(
        pool, n_hands=50, hand_size=7, rng=random.Random(0)
    )
    # 7 lands in every hand → outside [2, 5] → 0% keep_rate.
    assert stats.land_min == 7
    assert stats.land_max == 7
    assert stats.keep_rate == 0.0


def test_all_spells_zero_percent_keep() -> None:
    """No lands → every hand has 0 lands → below the 2-land floor → keep=0."""
    pool = _make_pool(n_lands=0, n_spells=40)
    stats = sample_hands(
        pool, n_hands=50, hand_size=7, rng=random.Random(0)
    )
    assert stats.land_min == 0
    assert stats.land_max == 0
    assert stats.keep_rate == 0.0


def test_balanced_deck_produces_reasonable_keep_rate() -> None:
    """A 38-land / 61-card deck has plenty of keepable hands."""
    pool = _make_pool(n_lands=38, n_spells=61)
    stats = sample_hands(
        pool, n_hands=2000, hand_size=7, rng=random.Random(0)
    )
    # Empirically ~0.7 on this shape; sanity check bounds.
    assert 0.5 < stats.keep_rate < 0.9
    assert stats.land_mean > 2.0
    assert stats.land_mean < 3.5


def test_seed_reproducibility() -> None:
    """Same seed → identical HandStats."""
    pool = _make_pool(n_lands=38, n_spells=61)
    a = sample_hands(pool, n_hands=100, hand_size=7, rng=random.Random(42))
    b = sample_hands(pool, n_hands=100, hand_size=7, rng=random.Random(42))
    assert a == b


def test_different_seeds_produce_different_streams() -> None:
    """Distinct seeds diverge (statistical guard against constant streams)."""
    pool = _make_pool(n_lands=38, n_spells=61)
    a = sample_hands(pool, n_hands=100, hand_size=7, rng=random.Random(1))
    b = sample_hands(pool, n_hands=100, hand_size=7, rng=random.Random(9999))
    # At least one aggregate stat should differ.
    assert (a.keep_rate, a.land_mean) != (b.keep_rate, b.land_mean)


def test_empty_pool_returns_zero_stats() -> None:
    stats = sample_hands([], n_hands=100, hand_size=7, rng=random.Random(0))
    assert stats.hands == 0
    assert stats.keep_rate == 0.0


def test_hand_size_exceeds_pool_returns_zero_stats() -> None:
    pool = _make_pool(n_lands=3, n_spells=3)  # 6 cards
    stats = sample_hands(pool, n_hands=100, hand_size=7, rng=random.Random(0))
    assert stats.hands == 0


def test_zero_hands_returns_zero_stats() -> None:
    pool = _make_pool(n_lands=38, n_spells=61)
    stats = sample_hands(pool, n_hands=0, hand_size=7, rng=random.Random(0))
    assert stats.hands == 0


def test_cmc_le_3_counts_cheap_spells() -> None:
    """cmc<=3 mean scales with proportion of cheap spells in the pool.

    All-Bolts (cmc=3) pool → every drawn card is cmc<=3 → mean == hand_size.
    (Lands with cmc=0 also count as <=3, so a 0-land 100% Bolt pool: mean=7.)
    """
    pool = [_card("Bolt", 3, False)] * 60
    stats = sample_hands(pool, n_hands=100, hand_size=7, rng=random.Random(0))
    assert stats.cmc_le_3_mean == 7.0


def test_high_cmc_pool_has_low_cheap_mean() -> None:
    """All-8-drops → cmc<=3 mean ~= 0.

    (0 lands, no cheap spells — the whole hand is cmc=8.)
    """
    pool = [_card("Big", 8, False)] * 60
    stats = sample_hands(pool, n_hands=100, hand_size=7, rng=random.Random(0))
    assert stats.cmc_le_3_mean == 0.0


def test_land_median_between_min_and_max() -> None:
    pool = _make_pool(n_lands=38, n_spells=61)
    stats = sample_hands(
        pool, n_hands=500, hand_size=7, rng=random.Random(0)
    )
    assert stats.land_min <= stats.land_median <= stats.land_max


def test_hand_stats_dataclass_shape() -> None:
    """Pin the field set so router serialization stays stable."""
    stats = HandStats(
        hands=1,
        hand_size=7,
        land_min=2,
        land_max=5,
        land_median=3.0,
        land_mean=3.0,
        cmc_le_3_mean=2.5,
        keep_rate=1.0,
    )
    assert stats.hands == 1
    assert stats.keep_rate == 1.0
