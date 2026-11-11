"""D15.34 — `combo:"X"` predicate matches cards that appear in a combo
alongside card ``X`` (resolved by name).

Distinguished from the D14.2 `combo:<slug>` form via the tokenizer's
``value_quoted`` flag: bare RHS (D14.2) is a Spellbook combo id; quoted
RHS (D15.34) is a card name. `combo:has` sentinel is unchanged.
"""

from __future__ import annotations

from biblioplex.dsl.compiler import compile_ast
from biblioplex.dsl.parser import parse
from tests.dsl.test_compiler import cards_table, render


def test_d15_34_combo_quoted_x_resolves_card_name_via_subquery() -> None:
    """`combo:"Sol Ring"` — quoted RHS triggers card-name resolution.

    Compiled shape: EXISTS over combos where combos.oracle_ids @>
    ARRAY[cards.oracle_id] AND combos.oracle_ids @> ARRAY[<resolved oracle_id>].
    The RHS resolves via an inline `SELECT oracle_id FROM cards WHERE
    LOWER(name) = LOWER(:x)` subquery so no compile-time DB call is
    needed; unresolved cards fall through 3VL (NULL @> ARRAY[NULL] → NULL
    → EXISTS false).
    """
    cards = cards_table()
    sql = render(compile_ast(parse('combo:"Sol Ring"'), cards))
    assert "EXISTS" in sql
    # Must reference the combos table + oracle_ids column.
    assert "combos" in sql
    assert "oracle_ids" in sql
    # Must include a card-name resolution subquery: LOWER(name) = LOWER(:x).
    assert "lower(" in sql.lower()
    # RHS lowercased in the bound value.
    assert "'sol ring'" in sql.lower()


def test_d15_34_combo_quoted_x_binds_card_name_lowered() -> None:
    """Quoted RHS is lowered on the SQL side via LOWER(:x) — case-insensitive."""
    cards = cards_table()
    sql = render(compile_ast(parse('combo:"ISOCHRON SCEPTER"'), cards))
    assert "EXISTS" in sql
    # Confirms the SQL-side LOWER on the bound param.
    assert "lower(" in sql.lower()


def test_d15_34_combo_bare_slug_unchanged_from_d14_2() -> None:
    """`combo:<slug>` (bare) keeps D14.2 combo-id lookup — no regression."""
    cards = cards_table()
    sql = render(compile_ast(parse("combo:spellbook-123"), cards))
    assert "EXISTS" in sql
    assert "'spellbook-123'" in sql
    # Bare form does NOT trigger the card-name resolution subquery.
    assert "cards.name" not in sql.lower()


def test_d15_34_combo_has_sentinel_unchanged() -> None:
    """`combo:has` sentinel unchanged — no literal 'has' comparison."""
    cards = cards_table()
    sql = render(compile_ast(parse("combo:has"), cards))
    assert "EXISTS" in sql
    assert "'has'" not in sql


def test_d15_34_combo_quoted_has_treated_as_card_name_not_sentinel() -> None:
    """`combo:"has"` — quoted 'has' is a card name (`Has`?), not the sentinel.

    The sentinel path fires only on the bare unquoted `has`; quoting
    escapes into card-name mode. The pin protects against a future
    optimizer's temptation to strip quotes before the sentinel check.
    """
    cards = cards_table()
    sql = render(compile_ast(parse('combo:"has"'), cards))
    # Card-name path renders the resolution subquery.
    assert "lower(" in sql.lower()
    assert "'has'" in sql.lower()
