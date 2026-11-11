"""Regex `/pattern/` compiler tests (D3.14, §5.1).

Bare `/pattern/` already targets `name_search`. KeyValue variants
(`name:/pat/`, `o:/pat/`, `t:/pat/`) must route to the right `_search`
column via Postgres `~*` (case-insensitive).
"""

from __future__ import annotations

from biblioplex.dsl.compiler import compile_ast
from biblioplex.dsl.parser import parse
from tests.dsl.test_compiler import cards_table, render


def test_bare_regex_targets_name_search() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("/bolt$/"), cards))
    assert "name_search" in sql
    assert "~*" in sql
    assert "bolt$" in sql


def test_name_regex() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("name:/lightning/"), cards))
    assert "name_search" in sql
    assert "~*" in sql


def test_oracle_regex() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("o:/draws? a card/"), cards))
    assert "oracle_search" in sql
    assert "~*" in sql


def test_type_regex() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("t:/^Creature/"), cards))
    assert "type_search" in sql
    assert "~*" in sql


def test_regex_does_not_wrap_with_percent() -> None:
    # Regex pattern goes verbatim — no `%pattern%` wrapping.
    cards = cards_table()
    sql = render(compile_ast(parse("o:/exact/"), cards))
    # The ILIKE form would inject %% around the pattern. Regex must not.
    assert "%exact%" not in sql
