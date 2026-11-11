"""D15.35 — `has:combo` — card appears in at least one combo row.

Semantically identical to the D14.2 `combo:has` sentinel; the `has:`
prefix is where agents look first for "does this card have X",
mirroring Scryfall's `has:foil` / `has:watermark` conventions.
"""

from __future__ import annotations

import pytest

from biblioplex.dsl.compiler import UnsupportedOperatorError, compile_ast
from biblioplex.dsl.parser import parse
from tests.dsl.test_compiler import cards_table, render


def test_d15_35_has_combo_compiles_to_exists_over_combos() -> None:
    """`has:combo` renders EXISTS on combos.oracle_ids @>-shape membership."""
    cards = cards_table()
    sql = render(compile_ast(parse("has:combo"), cards))
    assert "EXISTS" in sql
    assert "combos" in sql
    assert "oracle_ids" in sql
    # Membership predicate: cards.oracle_id = ANY(co.oracle_ids).
    assert "any(" in sql.lower()


def test_d15_35_has_combo_case_insensitive_value() -> None:
    """`has:COMBO` matches — RHS folds to lower before lookup."""
    cards = cards_table()
    sql = render(compile_ast(parse("has:COMBO"), cards))
    assert "EXISTS" in sql
    assert "combos" in sql


def test_d15_35_has_combo_combines_with_color_filter() -> None:
    """`has:combo c:r` AND-chains with the color-identity filter."""
    cards = cards_table()
    sql = render(compile_ast(parse("has:combo c:r"), cards))
    assert "EXISTS" in sql
    assert " AND " in sql
    # `c:r` compiles against colors_eff.
    assert "colors_eff" in sql


def test_d15_35_has_unknown_value_still_rejected() -> None:
    """`has:` with an unknown value still raises `UnsupportedOperatorError`.

    Guards against a future refactor accidentally opening `has:X` to any
    value once `combo` was wired in.
    """
    cards = cards_table()
    with pytest.raises(UnsupportedOperatorError):
        compile_ast(parse("has:no_such_field"), cards)
