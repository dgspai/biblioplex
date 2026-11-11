"""`otag:` / `function:` operator tests (§5.1).

Compiles to ``EXISTS`` against the ``oracle_tags`` side table on
``oracle_id`` + lowercased slug. Hierarchy is pre-flattened at ingest
so the per-query SQL is a flat indexed equality.
"""

from __future__ import annotations

from biblioplex.dsl.compiler import compile_ast
from biblioplex.dsl.parser import parse
from tests.dsl.test_compiler import cards_table, render


def test_otag_compiles_to_exists_on_oracle_tags() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("otag:removal"), cards))
    assert "EXISTS" in sql
    assert "oracle_tags" in sql
    assert "oracle_id" in sql
    assert "'removal'" in sql


def test_function_alias_works() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("function:ramp"), cards))
    assert "EXISTS" in sql
    assert "'ramp'" in sql


def test_otag_value_lowercased() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("otag:Removal"), cards))
    assert "'removal'" in sql
    assert "'Removal'" not in sql


def test_otag_combines_with_color() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("otag:removal c:r"), cards))
    assert "EXISTS" in sql
    assert "'removal'" in sql
    assert "colors_eff" in sql
    assert " AND " in sql
