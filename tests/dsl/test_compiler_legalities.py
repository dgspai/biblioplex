"""Legalities compiler tests (D3.9, §5.1).

`f:`/`format:X` → `legalities->>X = 'legal'`.
`banned:X`     → `legalities->>X = 'banned'`.
`restricted:X` → `legalities->>X = 'restricted'`.
"""

from __future__ import annotations

from biblioplex.dsl.compiler import compile_ast
from biblioplex.dsl.parser import parse
from tests.dsl.test_compiler import cards_table, render


def test_f_alias() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("f:modern"), cards))
    assert "legalities" in sql
    assert "'modern'" in sql
    assert "'legal'" in sql


def test_format_alias_matches_f() -> None:
    cards = cards_table()
    a = render(compile_ast(parse("f:modern"), cards))
    b = render(compile_ast(parse("format:modern"), cards))
    assert a == b


def test_banned() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("banned:legacy"), cards))
    assert "legalities" in sql
    assert "'legacy'" in sql
    assert "'banned'" in sql


def test_restricted() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("restricted:vintage"), cards))
    assert "legalities" in sql
    assert "'vintage'" in sql
    assert "'restricted'" in sql


def test_format_value_lowercased() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("f:Modern"), cards))
    assert "'modern'" in sql
