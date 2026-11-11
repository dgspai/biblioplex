"""End-to-end boolean composition tests (D3.13, §5.1).

Verifies that implicit AND, explicit `OR`, leading `-` negation, and
parentheses still compose cleanly when combined with the D3.5-D3.12
operators (mana, numerics, is:, has:, legalities, rarity, kw:, etc.).

These are integration tests against the compiler — no DB needed.
"""

from __future__ import annotations

from biblioplex.dsl.compiler import compile_ast
from biblioplex.dsl.parser import parse
from tests.dsl.test_compiler import cards_table, render


def test_implicit_and_across_operators() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("t:creature c:rg cmc<=3"), cards))
    # Three operands → two ANDs.
    assert sql.upper().count(" AND ") >= 2
    assert "type_search" in sql
    assert "colors_eff" in sql
    assert "cmc" in sql


def test_explicit_or_across_operators() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("kw:flying OR kw:trample"), cards))
    assert " OR " in sql
    assert sql.lower().count("keywords") >= 2


def test_negation_on_is() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("-is:commander"), cards))
    assert "NOT " in sql
    assert "is_commander" in sql


def test_parens_group_precedence() -> None:
    cards = cards_table()
    # (c:r OR c:u) t:creature → AND-of-OR shape.
    sql = render(compile_ast(parse("(c:r OR c:u) t:creature"), cards))
    assert " OR " in sql
    assert " AND " in sql
    assert "type_search" in sql


def test_negation_on_legality() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("-f:standard"), cards))
    # SQLAlchemy collapses NOT (x = 'legal') to (x != 'legal'); either form
    # is correct so accept both.
    assert "NOT " in sql or "!=" in sql or "<>" in sql
    assert "legalities" in sql
    assert "'standard'" in sql


def test_or_with_mana_and_rarity() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("m:WW OR r:mythic"), cards))
    assert " OR " in sql
    assert "EXISTS" in sql.upper()
    assert "rarity" in sql


def test_complex_query_kitchen_sink() -> None:
    cards = cards_table()
    q = "t:creature c<=rg (kw:flying OR kw:trample) -is:commander cmc<=4 f:modern"
    sql = render(compile_ast(parse(q), cards))
    # Just verifies the whole tree compiles to legal SQL fragments.
    for fragment in ("type_search", "colors_eff", "keywords", "is_commander", "cmc", "legalities"):
        assert fragment in sql
    assert " OR " in sql
    assert "NOT " in sql


def test_negation_on_mana() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("-m:R"), cards))
    assert "NOT " in sql
    assert "EXISTS" in sql.upper()


def test_or_negation_mix() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("-c:r OR -c:u"), cards))
    assert " OR " in sql
    assert sql.upper().count("NOT ") >= 2
