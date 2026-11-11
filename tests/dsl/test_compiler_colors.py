"""Color set-op compiler tests (D3.4, §5.4).

Covers `c:` / `color:` against `colors_eff` and the identical operators on
`id:` / `identity:` against `color_identity`. Nicknames (guild, shard, wedge,
4-color) expand to letter sets per the static map in `colors.py`.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from biblioplex.dsl.colors import COLOR_NICKNAMES, parse_color_letters
from biblioplex.dsl.compiler import UnsupportedOperatorError, compile_ast
from biblioplex.dsl.parser import parse
from tests.dsl.test_compiler import cards_table, render

# ---- color-letter parsing ----


def test_letters_from_bare_letters() -> None:
    assert parse_color_letters("rg") == frozenset({"R", "G"})


def test_letters_case_insensitive() -> None:
    assert parse_color_letters("WuB") == frozenset({"W", "U", "B"})


def test_letters_from_nickname() -> None:
    # Boros = R, W. (Per Scryfall guild map.)
    assert parse_color_letters("boros") == frozenset({"R", "W"})


def test_letters_from_shard() -> None:
    # Esper = W, U, B.
    assert parse_color_letters("esper") == frozenset({"W", "U", "B"})


def test_letters_unknown_raises() -> None:
    with pytest.raises(ValueError):
        parse_color_letters("xyz")


# ---- compile to SQL ----


def test_c_colon_superset() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("c:rg"), cards))
    # superset: colors_eff @> ARRAY['R','G']
    assert "@>" in sql
    assert "colors_eff" in sql
    assert "'R'" in sql and "'G'" in sql


def test_c_eq_equal_set() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("c=rg"), cards))
    # equality on the array column
    assert "colors_eff =" in sql
    assert "'R'" in sql and "'G'" in sql


def test_c_eq_query_side_matches_ingest_wubrg_sort() -> None:
    """D10.2: `c=WUR` compiles to an array literal in canonical WUBRG order
    so it matches a row whose `colors_eff` was WUBRG-sorted at ingest.

    Postgres array `=` is ORDERED. The §5.4 drift resolution adopts WUBRG
    sort on both sides — ingest writes `colors_eff` WUBRG-sorted; the
    compiler emits the query-side literal in WUBRG order too. Before D10.2
    the compiler sorted alphabetically and ingest preserved face order, so
    a multi-face card with `colors_eff=['U','R','W']` missed `c=WUR`
    (compiler produced `['R','U','W']`).

    Behavioral check (matching against a real DB) is out of scope here —
    no DB fixture for compiler tests. This pin asserts the rendered literal
    is the WUBRG order so any drift on either side surfaces in diff.
    """
    cards = cards_table()
    sql = render(compile_ast(parse("c=WUR"), cards))
    assert "colors_eff =" in sql
    # WUBRG order for letters {W,U,R} is W,U,R — array literal must reflect that.
    # `postgresql.array(['W','U','R'])` renders as `ARRAY['W', 'U', 'R']` (with
    # the CAST applied), so the substring below is unambiguous.
    assert "ARRAY['W', 'U', 'R']" in sql


def test_c_eq_wubrg_sort_5c() -> None:
    """5c WUBRG canonical order: `c=rainbow` → ARRAY['W','U','B','R','G']."""
    cards = cards_table()
    sql = render(compile_ast(parse("c=wubrg"), cards))
    assert "ARRAY['W', 'U', 'B', 'R', 'G']" in sql


def test_c_gte_superset() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("c>=uw"), cards))
    assert "@>" in sql


def test_c_lte_subset() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("c<=esper"), cards))
    # subset: colors_eff <@ ARRAY['W','U','B']
    assert "<@" in sql
    assert "'W'" in sql and "'U'" in sql and "'B'" in sql


def test_c_gt_strict_superset() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("c>rg"), cards))
    assert "@>" in sql
    # strict means colors_eff != ARRAY[...]
    assert "<>" in sql or "!=" in sql


def test_c_lt_strict_subset() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("c<esper"), cards))
    assert "<@" in sql
    assert "<>" in sql or "!=" in sql


def test_c_multicolor_alias_m() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("c:m"), cards))
    assert "cardinality" in sql.lower()


def test_c_multicolor_alias_word() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("c:multicolor"), cards))
    assert "cardinality" in sql.lower()


def test_c_colorless() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("c:c"), cards))
    # cardinality = 0, or equality with empty array — either is fine.
    assert "colors_eff" in sql


def test_c_colorless_alias_word() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("c:colorless"), cards))
    assert "colors_eff" in sql


def test_c_numeric_count() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("c=2"), cards))
    assert "cardinality" in sql.lower()
    assert "= 2" in sql


# ---- identity mirrors c: ----


def test_id_superset() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("id:rg"), cards))
    assert "color_identity" in sql
    assert "@>" in sql


def test_identity_subset() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("identity<=esper"), cards))
    assert "color_identity" in sql
    assert "<@" in sql


# ---- property: set-op laws ----


_COLOR_LETTERS = ["W", "U", "B", "R", "G"]


@given(st.sets(st.sampled_from(_COLOR_LETTERS), min_size=1, max_size=5))
def test_property_letters_round_trip(letters: set[str]) -> None:
    # Lower-cased letters concatenated should round-trip back to the set.
    parsed = parse_color_letters("".join(sorted(letters)).lower())
    assert parsed == frozenset(letters)


@given(st.sampled_from(sorted(COLOR_NICKNAMES.keys())))
def test_property_nickname_round_trip(nick: str) -> None:
    # Each nickname expands deterministically and re-parses through `c:`.
    letters = parse_color_letters(nick)
    assert letters == frozenset(COLOR_NICKNAMES[nick])


# ---- commander: mirrors id<= ----


def test_commander_letters_subset() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("commander:WUB"), cards))
    assert "color_identity" in sql
    assert "<@" in sql
    assert "'W'" in sql and "'U'" in sql and "'B'" in sql


def test_commander_nickname_subset() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("commander:esper"), cards))
    assert "color_identity" in sql
    assert "<@" in sql


def test_commander_colorless() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("commander:c"), cards))
    assert "cardinality" in sql.lower()
    assert "color_identity" in sql


def test_commander_rejects_eq() -> None:
    with pytest.raises(UnsupportedOperatorError) as exc:
        compile_ast(parse("commander=WUB"), cards_table())
    assert str(exc.value) == "unsupported operator: commander="


def test_commander_rejects_lte() -> None:
    with pytest.raises(UnsupportedOperatorError) as exc:
        compile_ast(parse("commander<=WUB"), cards_table())
    assert str(exc.value) == "unsupported operator: commander<="


def test_commander_composes_with_is_commander_and_legal() -> None:
    cards = cards_table()
    sql = render(
        compile_ast(parse("is:commander commander:WUB f:commander"), cards)
    )
    assert "is_commander" in sql
    assert "color_identity" in sql
    assert "legalities" in sql
