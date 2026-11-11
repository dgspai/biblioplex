"""D15.22 `edhrec:` / `edhrecrank:` DSL predicate.

Numeric comparator against ``cards.edhrec_rank``. Backed by
``cards_edhrec_rank_idx`` (ASC NULLS LAST). Spec §5.2 previously listed
these keys as out-of-DSL; D15.22 promotes them to filters since the
column already exists and the sort key surfaces it.

Semantic: ``edhrec<10000`` = "top 10k played." NULL-ranked rows never
match a positive numeric comparison (SQL three-valued logic).
"""

from __future__ import annotations

from biblioplex.dsl.compiler import compile_ast
from biblioplex.dsl.parser import parse
from tests.dsl.test_compiler import cards_table, render


def test_edhrec_lt() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("edhrec<10000"), cards))
    assert "edhrec_rank" in sql
    assert "< 10000" in sql


def test_edhrec_gte() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("edhrec>=500"), cards))
    assert "edhrec_rank" in sql
    assert ">= 500" in sql


def test_edhrec_eq() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("edhrec=1"), cards))
    assert "edhrec_rank" in sql
    assert "= 1" in sql


def test_edhrec_neq() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("edhrec!=1"), cards))
    assert "edhrec_rank" in sql
    assert "!= 1" in sql or "<> 1" in sql


def test_edhrec_colon_is_eq() -> None:
    # `:` on a numeric key aliases to `=` (mirrors cmc:3 == cmc=3).
    cards = cards_table()
    a = render(compile_ast(parse("edhrec:3"), cards))
    b = render(compile_ast(parse("edhrec=3"), cards))
    assert a == b


def test_edhrecrank_alias() -> None:
    cards = cards_table()
    a = render(compile_ast(parse("edhrecrank<100"), cards))
    b = render(compile_ast(parse("edhrec<100"), cards))
    assert a == b


def test_edhrec_non_numeric_value_is_false() -> None:
    # Non-numeric RHS compiles to false (mirrors pow=X handling).
    cards = cards_table()
    sql = render(compile_ast(parse("edhrec=abc"), cards))
    assert "false" in sql.lower()
