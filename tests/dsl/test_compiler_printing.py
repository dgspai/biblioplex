"""D15.33 — Scryfall-parity printing-level keys (§5.1).

Every predicate binds against `raw->>'<field>'` on the OLDEST printing
per oracle_id — reprints in later sets don't surface via these keys
(oracle-collapse caveat, documented in tech_spec.md §1.3).
"""

from __future__ import annotations

from biblioplex.dsl.compiler import compile_ast
from biblioplex.dsl.parser import parse
from tests.dsl.test_compiler import cards_table, render

# ---- set: ----


def test_set_binds_lowered_code() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("set:war"), cards))
    assert "set" in sql
    assert "'war'" in sql
    assert "lower(" in sql.lower()


def test_set_case_insensitive() -> None:
    cards = cards_table()
    sql_upper = render(compile_ast(parse("set:WAR"), cards))
    sql_lower = render(compile_ast(parse("set:war"), cards))
    assert sql_upper == sql_lower


# ---- year: ----


def test_year_numeric_comparator_ge() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("year>=2020"), cards))
    assert "extract" in sql.lower()
    assert "released_at" in sql
    assert "2020" in sql


def test_year_non_numeric_rhs_compiles_to_false() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("year:abc"), cards))
    # sa.false() renders as "false" or "1 != 1".
    assert "false" in sql.lower() or "1 != 1" in sql


# ---- released: ----


def test_released_equal_date() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("released:2024-01-15"), cards))
    assert "released_at" in sql
    assert "2024-01-15" in sql


def test_released_ge_binds_correctly() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("released>=2024-01-01"), cards))
    assert ">=" in sql
    assert "2024-01-01" in sql


def test_released_malformed_rhs_compiles_to_false() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("released:not-a-date"), cards))
    assert "false" in sql.lower() or "1 != 1" in sql


# ---- artist: ----


def test_artist_ilike() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("artist:seb-mckinnon"), cards))
    assert "artist" in sql
    assert "ILIKE" in sql.upper() or "ilike" in sql.lower()


def test_artist_short_alias_a() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("a:mckinnon"), cards))
    assert "artist" in sql


# ---- flavor: ----


def test_flavor_ilike_on_flavor_text() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse('flavor:"lava"'), cards))
    assert "flavor_text" in sql
    assert "lava" in sql


# ---- frame: ----


def test_frame_binds_value() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("frame:2015"), cards))
    assert "frame" in sql
    assert "'2015'" in sql
