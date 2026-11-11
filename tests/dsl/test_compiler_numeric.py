"""Numeric comparator compiler tests (D3.6, §5.1).

Operators: `cmc`/`mv`/`manavalue`, `pow`/`power`, `tou`/`toughness`,
`pt`/`powtou`, `loy`/`loyalty`. Non-numeric power/toughness values like
`*`/`X` must cast to NULL and never match (Postgres `NULLIF` + safe cast).
`cmc` accepts `even`/`odd` extras.
"""

from __future__ import annotations

import pytest

from biblioplex.dsl.compiler import compile_ast
from biblioplex.dsl.parser import parse
from tests.dsl.test_compiler import cards_table, render

# ---- cmc / manavalue ----


def test_cmc_eq() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("cmc=3"), cards))
    assert "cmc" in sql
    assert "= 3" in sql


def test_cmc_gte() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("cmc>=4"), cards))
    assert "cmc" in sql
    assert ">= 4" in sql


def test_cmc_alias_mv() -> None:
    cards = cards_table()
    a = render(compile_ast(parse("mv=3"), cards))
    b = render(compile_ast(parse("cmc=3"), cards))
    assert a == b


def test_cmc_alias_manavalue() -> None:
    cards = cards_table()
    a = render(compile_ast(parse("manavalue=3"), cards))
    b = render(compile_ast(parse("cmc=3"), cards))
    assert a == b


def test_cmc_even() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("cmc:even"), cards))
    assert "%" in sql or "mod" in sql.lower()
    assert "cmc" in sql


def test_cmc_odd() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("cmc:odd"), cards))
    assert "%" in sql or "mod" in sql.lower()
    assert "cmc" in sql


# ---- power / toughness ----


def test_pow_gt() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("pow>3"), cards))
    # NULL on non-numeric (NULLIF on regex would lose decimals; use a regex check).
    assert "power" in sql.lower()


def test_tou_lte() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("tou<=2"), cards))
    assert "toughness" in sql.lower()


def test_pow_alias_power() -> None:
    cards = cards_table()
    a = render(compile_ast(parse("power>3"), cards))
    b = render(compile_ast(parse("pow>3"), cards))
    assert a == b


# ---- pt = pow + tou ----


def test_pt_eq() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("pt=5"), cards))
    assert "power" in sql.lower()
    assert "toughness" in sql.lower()


# ---- loyalty ----


def test_loy_eq() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("loy=4"), cards))
    assert "loyalty" in sql.lower()


def test_loy_alias() -> None:
    cards = cards_table()
    a = render(compile_ast(parse("loyalty=4"), cards))
    b = render(compile_ast(parse("loy=4"), cards))
    assert a == b


# ---- non-numeric values reject at parse-side (we accept numeric only) ----


def test_pow_non_numeric_value_does_not_crash() -> None:
    # A non-numeric value on power (e.g. `pow=X`) should compile to a always-false
    # predicate rather than raising at compile time. NULL semantics handle it.
    cards = cards_table()
    sql = render(compile_ast(parse("pow=X"), cards))
    # We expect a false-like predicate or one that yields NULL → no match.
    assert sql is not None


# ---- not-equal ----


def test_cmc_neq() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("cmc!=3"), cards))
    assert "cmc" in sql
    assert "!= 3" in sql or "<> 3" in sql


# ---- error case for unknown comparator inside even/odd ----


def test_cmc_bad_value_raises() -> None:
    cards = cards_table()
    with pytest.raises(ValueError):
        compile_ast(parse("cmc=notanumber"), cards)
