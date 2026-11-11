"""Tests for `dsl.mana.parse_cost_symbols` (D3.5, §5.5).

Generic numerics expand to a `GENERIC` sentinel. Hybrid / phyrexian symbols
stay composite. Bare-letter shorthand (`WW`, `2WW`) is accepted.
"""

from __future__ import annotations

from collections import Counter

from biblioplex.dsl.mana import GENERIC, parse_cost_symbols


def test_empty_returns_empty_counter() -> None:
    assert parse_cost_symbols("") == Counter()
    assert parse_cost_symbols("   ") == Counter()


def test_single_white_pip() -> None:
    assert parse_cost_symbols("{W}") == Counter({"W": 1})


def test_generic_expands() -> None:
    # {2} → two GENERIC pips.
    assert parse_cost_symbols("{2}") == Counter({GENERIC: 2})


def test_compound_cost() -> None:
    # {2}{W}{W} → {1,1,W,W}
    assert parse_cost_symbols("{2}{W}{W}") == Counter({GENERIC: 2, "W": 2})


def test_bare_letter_shorthand() -> None:
    assert parse_cost_symbols("WW") == Counter({"W": 2})


def test_bare_digit_shorthand() -> None:
    assert parse_cost_symbols("2WW") == Counter({GENERIC: 2, "W": 2})


def test_hybrid_stays_composite() -> None:
    assert parse_cost_symbols("{W/U}") == Counter({"W/U": 1})


def test_phyrexian_stays_composite() -> None:
    assert parse_cost_symbols("{W/P}") == Counter({"W/P": 1})


def test_two_brid_generic_stays_composite() -> None:
    assert parse_cost_symbols("{2/W}") == Counter({"2/W": 1})


def test_case_insensitive_braced() -> None:
    assert parse_cost_symbols("{w}{u}") == Counter({"W": 1, "U": 1})


def test_case_insensitive_bare() -> None:
    assert parse_cost_symbols("ww") == Counter({"W": 2})


def test_x_symbol() -> None:
    assert parse_cost_symbols("{X}") == Counter({"X": 1})


def test_colorless_distinct_from_generic() -> None:
    # {C} (colorless) is its own symbol, NOT generic.
    cost = parse_cost_symbols("{C}{2}")
    assert cost == Counter({"C": 1, GENERIC: 2})
