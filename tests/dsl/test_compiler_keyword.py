"""D15.30 — `keyword:` / `kw:` compiler tests (§5.1).

Case-insensitive `keywords @> ARRAY[X]` via `lower(unnest(...))` EXISTS.
Comma-separated multi-value form ANDs across per-value predicates.
"""

from __future__ import annotations

from biblioplex.dsl.compiler import compile_ast
from biblioplex.dsl.parser import parse
from tests.dsl.test_compiler import cards_table, render


def test_keyword_single_value_exists() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("keyword:flying"), cards))
    assert "unnest" in sql.lower()
    assert "keywords" in sql
    assert "lower" in sql.lower()
    assert "'flying'" in sql


def test_kw_alias_matches_keyword() -> None:
    # D15.30 — `kw:` short alias should produce identical SQL.
    cards = cards_table()
    sql_kw = render(compile_ast(parse("kw:flying"), cards))
    sql_keyword = render(compile_ast(parse("keyword:flying"), cards))
    assert sql_kw == sql_keyword


def test_keyword_multi_value_ands() -> None:
    # D15.30 — `keyword:flying,trample` = AND across two EXISTS clauses.
    cards = cards_table()
    sql = render(compile_ast(parse("keyword:flying,trample"), cards))
    assert sql.upper().count("EXISTS") == 2
    assert "'flying'" in sql
    assert "'trample'" in sql
    assert " AND " in sql.upper()


def test_keyword_case_normalized_to_lowercase() -> None:
    # Case-insensitive matching: RHS is lowered before binding.
    cards = cards_table()
    sql = render(compile_ast(parse("keyword:FLYING"), cards))
    assert "'flying'" in sql


def test_keyword_negation() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("-keyword:flying"), cards))
    assert "NOT" in sql.upper()
    assert "'flying'" in sql


def test_keyword_empty_value_compiles_to_false() -> None:
    # Only empty tokens (e.g. `keyword:,`) → false. Guards against
    # inadvertently matching every row.
    cards = cards_table()
    sql = render(compile_ast(parse("keyword:,"), cards))
    assert "false" in sql.lower() or "1 != 1" in sql
