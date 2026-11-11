"""D15.23 `usd:` DSL predicate.

Numeric comparator against ``cards.price_usd`` — the MIN over all
printings of the same oracle_id, computed at collapse time (see D1.9 /
`schema.sql` comment). Backed by ``cards_price_usd_idx``.

Spec §5.2 previously listed `usd:` as out-of-DSL; D15.23 promotes it
to §5.1 for parity with the pre-existing `order=usd` sort and the
`budget_usd` gate on `/decks/upgrade` + `/decks/manabase`.

NULL-priced rows never match a positive numeric comparison (SQL
three-valued logic).
"""

from __future__ import annotations

from biblioplex.dsl.compiler import compile_ast
from biblioplex.dsl.parser import parse
from tests.dsl.test_compiler import cards_table, render


def test_usd_lt() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("usd<5"), cards))
    assert "price_usd" in sql
    assert "< 5" in sql


def test_usd_gte() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("usd>=1"), cards))
    assert "price_usd" in sql
    assert ">= 1" in sql


def test_usd_eq() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("usd=0"), cards))
    assert "price_usd" in sql
    assert "= 0" in sql


def test_usd_neq() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("usd!=0"), cards))
    assert "price_usd" in sql
    assert "!= 0" in sql or "<> 0" in sql


def test_usd_colon_is_eq() -> None:
    # `:` on a numeric key aliases to `=` (mirrors cmc:3 == cmc=3).
    cards = cards_table()
    a = render(compile_ast(parse("usd:2"), cards))
    b = render(compile_ast(parse("usd=2"), cards))
    assert a == b


def test_usd_accepts_decimals() -> None:
    # Prices are numeric(...,2); the DSL rhs must parse floats cleanly.
    cards = cards_table()
    sql = render(compile_ast(parse("usd<0.5"), cards))
    assert "price_usd" in sql
    assert "< 0.5" in sql


def test_usd_non_numeric_value_is_false() -> None:
    # Non-numeric RHS compiles to false (mirrors pow=X handling).
    cards = cards_table()
    sql = render(compile_ast(parse("usd=abc"), cards))
    assert "false" in sql.lower()
