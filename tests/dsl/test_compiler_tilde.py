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
