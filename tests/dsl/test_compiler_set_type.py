"""D15.32 — `is:funny` / `is:masters` / `is:token` compiler tests (§5.1).

Set-type-based `is:` predicates compile to `raw->>'set_type' = X`.
`is:token` here matches token-only printings via `set_type`, NOT via
`layout = 'token'` (which we don't map).
"""

from __future__ import annotations

import pytest

from biblioplex.dsl.compiler import compile_ast
from biblioplex.dsl.parser import parse
from tests.dsl.test_compiler import cards_table, render


@pytest.mark.parametrize(
    ("value", "expected_set_type"),
    [
        ("funny", "funny"),
        ("masters", "masters"),
        ("token", "token"),
    ],
)
def test_is_set_type_predicates(value: str, expected_set_type: str) -> None:
    cards = cards_table()
    sql = render(compile_ast(parse(f"is:{value}"), cards))
    assert "set_type" in sql
    assert f"'{expected_set_type}'" in sql


def test_is_token_uses_set_type_not_layout() -> None:
    # Guard: D15.32 spec explicitly says `is:token` compiles against
    # `set_type`, not `layout`. Regression pin: SQL references set_type.
    cards = cards_table()
    sql = render(compile_ast(parse("is:token"), cards))
    assert "set_type" in sql
    # `layout = 'token'` would NOT appear because layout isn't mapped.
    assert "layout = 'token'" not in sql


def test_is_funny_negated() -> None:
    # `-is:funny` = NOT (raw->>'set_type' = 'funny'). Note SQLAlchemy
    # optimizes NOT(X = Y) to X != Y, so we match either form.
    cards = cards_table()
    sql = render(compile_ast(parse("-is:funny"), cards))
    assert "set_type" in sql
    assert "'funny'" in sql
    assert "!=" in sql or "NOT" in sql.upper()
