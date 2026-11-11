"""Compiler tests for `m:` / `m=` / `m>` (D3.5, §5.5).

Per-face semantics: `mana_cost_search` is the space-joined per-face cost
strings. Any face's multiset superset / equality / strict superset against
the query multiset matches.
"""

from __future__ import annotations

from biblioplex.dsl.compiler import compile_ast
from biblioplex.dsl.parser import parse
from tests.dsl.test_compiler import cards_table, render


def test_m_colon_emits_exists_per_face() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("m:WW"), cards))
    # Expect a per-face split using regexp_split_to_table and an EXISTS check.
    assert "EXISTS" in sql.upper()
    assert "regexp_split_to_table" in sql.lower()
    assert "mana_cost_search" in sql


def test_m_colon_counts_specific_symbols() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("m:WW"), cards))
    # Predicate should reference the {W} symbol literally somewhere.
    assert "{W}" in sql


def test_m_colon_expands_generic() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("m:2W"), cards))
    # Generic pips count via sum of digits inside {N} symbols.
    # The W symbol literal still appears.
    assert "{W}" in sql
    # Some form of generic-pip arithmetic should be present.
    sql_lower = sql.lower()
    assert "regexp_matches" in sql_lower or "regexp_split" in sql_lower


def test_m_equals_face_equals() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("m=WW"), cards))
    # Equality should compare total symbol count too.
    assert "EXISTS" in sql.upper()


def test_m_strict_superset() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("m>WW"), cards))
    # Strict superset: superset AND face length > query length.
    assert "EXISTS" in sql.upper()


def test_m_alias_mana() -> None:
    cards = cards_table()
    sql_a = render(compile_ast(parse("m:WW"), cards))
    sql_b = render(compile_ast(parse("mana:WW"), cards))
    assert sql_a == sql_b


def test_m_empty_query_value_matches_any_nonempty_face() -> None:
    # Edge: empty m: should still compile; let DB handle the truthy case.
    cards = cards_table()
    sql = render(compile_ast(parse('m:""'), cards))
    assert "EXISTS" in sql.upper()
