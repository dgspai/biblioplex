"""Rarity comparator compiler tests (D3.10, §5.1).

`r:` / `rarity:` ordered comparison: common(0) < uncommon(1) < rare(2)
< special(3) < mythic(4) < bonus(5). All Scryfall comparators apply.
"""

from __future__ import annotations

from biblioplex.dsl.compiler import compile_ast
from biblioplex.dsl.parser import parse
from tests.dsl.test_compiler import cards_table, render


def test_rarity_eq() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("r:mythic"), cards))
    assert "rarity" in sql
    # Equality goes via either CASE rank == 4 or rarity == 'mythic'.
    assert "'mythic'" in sql or "= 4" in sql


def test_rarity_alias_rarity() -> None:
    cards = cards_table()
    a = render(compile_ast(parse("r:mythic"), cards))
    b = render(compile_ast(parse("rarity:mythic"), cards))
    assert a == b


def test_rarity_gte_rare() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("r>=rare"), cards))
    # Must compile ranking via CASE.
    assert "CASE" in sql.upper()
    assert ">=" in sql


def test_rarity_lt_mythic() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("r<mythic"), cards))
    assert "CASE" in sql.upper()
    assert "<" in sql


def test_rarity_neq() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("r!=common"), cards))
    assert "rarity" in sql
    assert "!=" in sql or "<>" in sql


def test_rarity_case_insensitive() -> None:
    cards = cards_table()
    a = render(compile_ast(parse("r:Mythic"), cards))
    b = render(compile_ast(parse("r:mythic"), cards))
    assert a == b
