"""Tests for the canonical WUBRG-cycle ``color_sort_key`` (§6.1).

Pins every one of the 32 positions in the cycle so the spec-drift
resolution doesn't quietly drift back.
"""

from __future__ import annotations

import pytest

from biblioplex.ingest.color_sort import color_sort_key

# Each (input, expected) pair is the cycle stated in the module docstring.
# Inputs are GIVEN in Scryfall's canonical WUBRG letter order — the lookup
# sorts internally so input order doesn't matter (see permutation tests below).
_CYCLE: list[tuple[list[str], int]] = [
    # 0 colorless
    ([], 0),
    # 1-5 mono
    (["W"], 1), (["U"], 2), (["B"], 3), (["R"], 4), (["G"], 5),
    # 6-10 2c allied (WU, UB, BR, RG, GW)
    (["W", "U"], 6),
    (["U", "B"], 7),
    (["B", "R"], 8),
    (["R", "G"], 9),
    (["W", "G"], 10),
    # 11-15 2c enemy (WB, BG, UR, RW, UG)
    (["W", "B"], 11),
    (["B", "G"], 12),
    (["U", "R"], 13),
    (["W", "R"], 14),
    (["U", "G"], 15),
    # 16-20 3c shards (WUB, UBR, BRG, RGW, GWU)
    (["W", "U", "B"], 16),
    (["U", "B", "R"], 17),
    (["B", "R", "G"], 18),
    (["W", "R", "G"], 19),
    (["W", "U", "G"], 20),
    # 21-25 3c wedges (WBG, URW, BGU, RWB, GUR)
    (["W", "B", "G"], 21),
    (["W", "U", "R"], 22),
    (["U", "B", "G"], 23),
    (["W", "B", "R"], 24),
    (["U", "R", "G"], 25),
    # 26-30 4c (missing W/U/B/R/G)
    (["U", "B", "R", "G"], 26),
    (["W", "B", "R", "G"], 27),
    (["W", "U", "R", "G"], 28),
    (["W", "U", "B", "G"], 29),
    (["W", "U", "B", "R"], 30),
    # 31 5c
    (["W", "U", "B", "R", "G"], 31),
]


@pytest.mark.parametrize("colors,expected", _CYCLE)
def test_cycle_position(colors: list[str], expected: int) -> None:
    """Every cycle slot maps to exactly the expected 0–31 position."""
    assert color_sort_key(colors) == expected


def test_cycle_is_a_bijection_over_0_to_31() -> None:
    """All 32 slots are distinct and densely occupy [0, 31] — no gaps, no dupes."""
    positions = [pos for _, pos in _CYCLE]
    assert sorted(positions) == list(range(32))


def test_none_input_is_colorless() -> None:
    """`None` (e.g. from `flat.get('colors_eff')` on a card without colors_eff)."""
    assert color_sort_key(None) == 0


def test_input_order_does_not_matter() -> None:
    """Inputs in non-WUBRG order normalize to the same position."""
    # GUW (Bant wedge-style spelling) → canonical WUG → position 20.
    assert color_sort_key(["G", "U", "W"]) == 20
    # BR-then-W reorders to WBR (Mardu wedge) → 24.
    assert color_sort_key(["R", "B", "W"]) == 24


def test_duplicates_are_deduped() -> None:
    """Dupes (defensive: shouldn't happen but mustn't bias the key)."""
    assert color_sort_key(["U", "U", "U"]) == 2
    assert color_sort_key(["W", "U", "W"]) == 6


def test_unknown_letters_dropped() -> None:
    """Letters outside WUBRG silently drop (e.g. legacy 'P' for phyrexian)."""
    # Just 'W' after dropping 'P'.
    assert color_sort_key(["W", "P"]) == 1
    # Fully unrecognized → colorless.
    assert color_sort_key(["Z", "Q"]) == 0
