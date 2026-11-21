"""`~` placeholder in `o:` substitutes top-level `name` (D3.12, §5.1)."""

from __future__ import annotations

from biblioplex.dsl.compiler import compile_ast
from biblioplex.dsl.parser import parse
from tests.dsl.test_compiler import cards_table, render


def test_o_tilde_substitutes_name_in_predicate() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("o:~"), cards))
    # SQL `replace(..., '~', cards.name)` must reference cards.name.
    assert "cards.name" in sql
    assert "REPLACE" in sql.upper() or "replace" in sql
    assert "oracle_search" in sql


def test_o_tilde_within_phrase() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse('o:"draws a card named ~"'), cards))
    assert "cards.name" in sql


def test_o_without_tilde_unchanged() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("o:flying"), cards))
    # No tilde, no name substitution.
    assert "cards.name" not in sql
    assert "oracle_search ILIKE" in sql


def test_tilde_matches_how_rules_text_names_the_card() -> None:
    """Oracle text says "this creature", the front face, or a legend's short name."""
    sql = render(compile_ast(parse('o:"when ~ enters"'), cards_table()))
    assert "this creature" in sql
    assert "split_part" in sql
    assert "' // '" in sql and "', '" in sql


def test_negated_tilde_never_yields_null() -> None:
    """A single-face card has no back-face name; NULL under NOT drops every row."""
    sql = render(compile_ast(parse('-o:"~ enters tapped"'), cards_table()))
    assert "coalesce" in sql.lower()
