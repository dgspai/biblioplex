"""`combo:` operator tests (§5.1, D14.2).

Compiles to ``EXISTS`` against the ``combos`` side table on
``oracle_id`` membership in ``oracle_ids``.

* ``combo:has`` — card appears in ANY combo.
* ``combo:<slug>`` — card appears in the combo with id ``<slug>``.

Non-``:`` operators (``combo>foo``, ``combo=foo``, ``combo<=foo``, …) are
rejected with :class:`UnsupportedOperatorError` and surface as 422 at the
HTTP layer.
"""

from __future__ import annotations

import pytest

from biblioplex.dsl.compiler import UnsupportedOperatorError, compile_ast
from biblioplex.dsl.parser import parse
from tests.dsl.test_compiler import cards_table, render


def test_combo_has_compiles_to_exists_on_combos_any() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("combo:has"), cards))
    assert "EXISTS" in sql
    assert "combos" in sql
    assert "oracle_ids" in sql
    assert "any(" in sql.lower()


def test_combo_slug_matches_id_and_membership() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("combo:spellbook-123"), cards))
    assert "EXISTS" in sql
    assert "combos" in sql
    assert "'spellbook-123'" in sql
    assert "oracle_ids" in sql
    assert "any(" in sql.lower()


def test_combo_slug_lowercased() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("combo:Isochron-Scepter"), cards))
    # slugs are normalized to lower-case so `combo:Foo` and `combo:foo`
    # match the same row (mirrors otag: semantics).
    assert "'isochron-scepter'" in sql
    assert "'Isochron-Scepter'" not in sql


def test_combo_has_case_insensitive() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("combo:HAS"), cards))
    # `combo:has` is a sentinel value, not a slug — case-insensitive so
    # users don't get 0 hits from a shift-key slip.
    assert "EXISTS" in sql
    # `has` is the sentinel — no literal 'has' comparison against `id`.
    assert "combos.id = 'has'" not in sql
    assert "'has'" not in sql


def test_combo_combines_with_color() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("combo:has c:r"), cards))
    assert "EXISTS" in sql
    assert "colors_eff" in sql
    assert " AND " in sql


@pytest.mark.parametrize("op", ["=", ">", "<", ">=", "<=", "!="])
def test_combo_rejects_non_colon_operators(op: str) -> None:
    cards = cards_table()
    with pytest.raises(UnsupportedOperatorError):
        compile_ast(parse(f"combo{op}foo"), cards)
