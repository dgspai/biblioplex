"""Pure-function tests for :mod:`biblioplex.decklist.manabase` (D14.6)."""

from __future__ import annotations

from biblioplex.decklist.manabase import (
    BASIC_NAMES,
    NONBASIC_PRIORITY,
    distribute_basics,
)


def test_mono_color_all_basics_that_color() -> None:
    """Mono-red pips → all Mountains."""
    result = distribute_basics({"R": 10}, 20)
    assert result == {"W": 0, "U": 0, "B": 0, "R": 20, "G": 0, "C": 0}


def test_colorless_c_pip_all_wastes() -> None:
    """D16.5: `{"C": 1}` distributes every slot to Wastes (the C-key)."""
    result = distribute_basics({"C": 1}, 30)
    assert result["C"] == 30
    assert all(result[c] == 0 for c in ("W", "U", "B", "R", "G"))


def test_even_split_two_colors() -> None:
    """Equal weights on two colors → equal split."""
    result = distribute_basics({"W": 3, "U": 3}, 20)
    assert result["W"] == 10
    assert result["U"] == 10
    assert result["B"] == 0


def test_hare_quota_rounding_fair() -> None:
    """Uneven weights round via largest-remainder — sum equals input."""
    result = distribute_basics({"W": 1, "U": 1, "B": 1}, 10)
    total = sum(result.values())
    assert total == 10
    # Each color gets 3 with 1 remaining seat; WUBRG order breaks the tie.
    assert result["W"] == 4  # W wins the leftover
    assert result["U"] == 3
    assert result["B"] == 3


def test_zero_count_returns_all_zero() -> None:
    """Zero-count → empty distribution."""
    result = distribute_basics({"R": 5}, 0)
    assert all(v == 0 for v in result.values())


def test_zero_weight_gets_zero_basics() -> None:
    """A color absent from pip_weights (or with weight 0) → 0 basics."""
    result = distribute_basics({"R": 5, "G": 5}, 10)
    assert result["W"] == 0
    assert result["U"] == 0
    assert result["B"] == 0
    assert result["R"] == 5
    assert result["G"] == 5


def test_no_pips_returns_zero() -> None:
    """Empty pip_weights → all zeros (no way to distribute)."""
    result = distribute_basics({}, 20)
    assert all(v == 0 for v in result.values())


def test_negative_pips_treated_as_zero() -> None:
    """Defensive: negative weights don't corrupt distribution."""
    result = distribute_basics({"W": -5, "U": 5}, 10)
    assert result["W"] == 0
    assert result["U"] == 10


def test_five_color_proportional() -> None:
    """WUBRG 1:2:3:4:5 → distribution matches proportions."""
    result = distribute_basics({"W": 1, "U": 2, "B": 3, "R": 4, "G": 5}, 15)
    # ideal: W=1, U=2, B=3, R=4, G=5 → sum 15, no rounding needed.
    assert result == {"W": 1, "U": 2, "B": 3, "R": 4, "G": 5, "C": 0}


def test_priority_list_non_empty_and_sane_order() -> None:
    """Regression pin: priority list carries the well-known top picks."""
    assert "Command Tower" in NONBASIC_PRIORITY[:10]
    assert "Reflecting Pool" in NONBASIC_PRIORITY[:10]
    # Shocklands are present.
    assert "Hallowed Fountain" in NONBASIC_PRIORITY
    # Utility lands are further down.
    assert NONBASIC_PRIORITY.index("Reliquary Tower") > NONBASIC_PRIORITY.index(
        "Command Tower"
    )


def test_basic_names_cover_wubrgc() -> None:
    """Basic-name map covers all five colors + colorless (D16.5)."""
    assert BASIC_NAMES == {
        "W": "Plains",
        "U": "Island",
        "B": "Swamp",
        "R": "Mountain",
        "G": "Forest",
        "C": "Wastes",
    }
