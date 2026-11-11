"""`is:` predicate compiler tests (D3.7, §5.1).

`is:commander` is a column lookup. `is:permanent|spell|vanilla|frenchvanilla`
must derive from TOP-LEVEL ``type_line`` / ``oracle_text`` (not the face-
flattened `_search` columns) per the §5.1 fix.
"""

from __future__ import annotations

import pytest

from biblioplex.dsl.compiler import compile_ast
from biblioplex.dsl.parser import parse
from biblioplex.scryfall import catalogs as scryfall_catalogs
from tests.dsl.test_compiler import cards_table, render


@pytest.fixture(autouse=True)
def _reset_catalog_cache():
    """Avoid cross-test leakage of the Scryfall keyword cache."""
    scryfall_catalogs.set_keyword_set(frozenset())
    yield
    scryfall_catalogs.set_keyword_set(frozenset())


def test_is_commander() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("is:commander"), cards))
    assert "is_commander" in sql
    assert "true" in sql.lower()


def test_is_permanent_uses_top_level_type_line() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("is:permanent"), cards))
    # type_line (NOT type_search) per spec fix.
    assert "type_line" in sql
    assert "type_search" not in sql


def test_is_permanent_covers_all_permanent_types() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("is:permanent"), cards)).lower()
    for word in ("artifact", "creature", "enchantment", "land", "planeswalker", "battle"):
        assert word in sql


def test_is_spell_is_nonland() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("is:spell"), cards))
    # NOT land
    assert "type_line" in sql
    assert "land" in sql.lower()
    assert "not" in sql.lower()


def test_is_vanilla_creature_with_empty_oracle() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("is:vanilla"), cards))
    assert "type_line" in sql
    assert "creature" in sql.lower()
    # empty oracle_text (NULL or '')
    assert "oracle_text" in sql


def test_is_frenchvanilla_does_not_crash() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("is:frenchvanilla"), cards))
    # Conservative implementation: derive from creature + oracle_text shape.
    # Test only that we produce SOME predicate, not the exact heuristic.
    assert sql is not None
    assert "type_line" in sql


def test_is_frenchvanilla_fallback_when_catalog_missing() -> None:
    """No cache → punctuation heuristic on top-level ``oracle_text``."""
    scryfall_catalogs.set_keyword_set(frozenset())
    cards = cards_table()
    sql = render(compile_ast(parse("is:frenchvanilla"), cards)).lower()
    assert "oracle_text" in sql
    # Heuristic uses `oracle_text ~ '[.:,]'` negated, not regexp_replace.
    assert "regexp_replace" not in sql


def test_is_reserved_compiles_to_raw_reserved_true() -> None:
    """`is:reserved` — `raw->>'reserved' = 'true'` (D15.20).

    Reserved-list membership is oracle-level (a name is on the RL or not,
    regardless of printing), so our oldest-printing collapse preserves it
    correctly. Backed by no explicit index — the RL is ~500 cards out of
    ~30k so a bitmap scan is cheap. Add a partial index if this ever
    shows up in slow-query logs.
    """
    cards = cards_table()
    sql = render(compile_ast(parse("is:reserved"), cards))
    lowered = sql.lower()
    assert "raw" in lowered
    assert "reserved" in lowered
    assert "'true'" in lowered


def test_is_reserved_negatable() -> None:
    """`-is:reserved` negates the predicate.

    SQLAlchemy compiles `NOT (x = 'true')` as `x != 'true'` — the
    absence of `NOT` in the string is a rendering artifact, not a
    semantic gap. Pin the operator flip instead.
    """
    cards = cards_table()
    sql = render(compile_ast(parse("-is:reserved"), cards)).lower()
    assert "reserved" in sql
    assert "!= 'true'" in sql


def test_is_frenchvanilla_uses_catalog_regex_when_cached() -> None:
    """Cache populated → strip reminder text, match against keyword regex."""
    scryfall_catalogs.set_keyword_set(
        frozenset({"flying", "vigilance", "trample"})
    )
    cards = cards_table()
    sql = render(compile_ast(parse("is:frenchvanilla"), cards)).lower()
    assert "regexp_replace" in sql
    assert "flying" in sql
    assert "vigilance" in sql
