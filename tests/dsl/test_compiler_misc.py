"""Misc operators compiler tests (D3.11, §5.1).

* `kw:` / `keyword:`  → `keywords @> ARRAY[X]` (case-insensitive lookup).
* `produces:`         → set-op semantics on `produced_mana`, same as `c:`.
* `has:indicator`     → `raw->>'color_indicator' IS NOT NULL`.
* `has:watermark`     → `raw->>'watermark'      IS NOT NULL`.
* `layout:X`          → `layout = X`.
"""

from __future__ import annotations

from biblioplex.dsl.compiler import compile_ast
from biblioplex.dsl.parser import parse
from tests.dsl.test_compiler import cards_table, render

# ---- kw: ----


def test_kw_uses_array_contains() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("kw:flying"), cards))
    assert "keywords" in sql
    # case-insensitive lookup goes through unnest + lower(), not raw @>.
    assert "lower" in sql.lower()
    assert "'flying'" in sql


def test_kw_case_insensitive() -> None:
    cards = cards_table()
    a = render(compile_ast(parse("kw:flying"), cards))
    b = render(compile_ast(parse("kw:Flying"), cards))
    # Both must normalize so the produced predicate is identical.
    assert a == b


def test_kw_alias() -> None:
    cards = cards_table()
    a = render(compile_ast(parse("kw:flying"), cards))
    b = render(compile_ast(parse("keyword:flying"), cards))
    assert a == b


# ---- produces: ----


def test_produces_superset() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("produces:rg"), cards))
    assert "produced_mana" in sql
    assert "@>" in sql


def test_produces_equal() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("produces=wu"), cards))
    assert "produced_mana =" in sql


def test_produces_subset() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("produces<=esper"), cards))
    assert "<@" in sql


def test_produces_colorless() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("produces:c"), cards))
    assert "produced_mana" in sql


def test_prod_alias_superset() -> None:
    # D15.28 — `prod:` alias for `produces:`.
    cards = cards_table()
    sql = render(compile_ast(parse("prod:rg"), cards))
    assert "produced_mana" in sql
    assert "@>" in sql


def test_prod_alias_multi_color_and() -> None:
    # D15.28 — prod:WU compiles to same array-containment as produces:WU.
    cards = cards_table()
    sql_prod = render(compile_ast(parse("prod:wu"), cards))
    sql_produces = render(compile_ast(parse("produces:wu"), cards))
    assert sql_prod == sql_produces


def test_prod_alias_colorless() -> None:
    # D15.28 — prod:c compiles same as produces:c (cardinality == 0).
    cards = cards_table()
    sql = render(compile_ast(parse("prod:c"), cards))
    assert "produced_mana" in sql
    assert "cardinality" in sql.lower()


# ---- has: ----


def test_has_indicator() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("has:indicator"), cards))
    assert "raw" in sql
    assert "color_indicator" in sql
    assert "IS NOT NULL" in sql.upper()


def test_has_watermark() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("has:watermark"), cards))
    assert "raw" in sql
    assert "watermark" in sql
    assert "IS NOT NULL" in sql.upper()


# ---- layout: ----


def test_layout_eq() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("layout:saga"), cards))
    assert "layout" in sql
    assert "'saga'" in sql


def test_layout_case_insensitive() -> None:
    cards = cards_table()
    a = render(compile_ast(parse("layout:Saga"), cards))
    b = render(compile_ast(parse("layout:saga"), cards))
    assert a == b
