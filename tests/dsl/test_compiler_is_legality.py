"""D15.31 — `is:banned` / `is:restricted` / `is:legal` compiler tests.

Compact form of the existing `banned:<fmt>` / `restricted:<fmt>` /
`f:<fmt>` keys, defaulting to the `commander` format. The spec's
`fmt:` context switch is intentionally out of scope for the v1 land
(filed as Spec Drift in todo.md) — agents needing a specific format
use the fmt-explicit keys.
"""

from __future__ import annotations

from biblioplex.dsl.compiler import compile_ast
from biblioplex.dsl.parser import parse
from tests.dsl.test_compiler import cards_table, render


def test_is_banned_default_commander() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("is:banned"), cards))
    assert "legalities" in sql
    assert "'commander'" in sql
    assert "'banned'" in sql


def test_is_restricted_default_commander() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("is:restricted"), cards))
    assert "legalities" in sql
    assert "'commander'" in sql
    assert "'restricted'" in sql


def test_is_legal_default_commander() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("is:legal"), cards))
    assert "legalities" in sql
    assert "'commander'" in sql
    assert "'legal'" in sql


def test_is_banned_negated() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("-is:banned"), cards))
    assert "'banned'" in sql
    assert "'commander'" in sql
    assert "!=" in sql or "NOT" in sql.upper()


def test_is_legal_and_kw_compound() -> None:
    # Compound: `is:legal keyword:flying` should compile both predicates.
    cards = cards_table()
    sql = render(compile_ast(parse("is:legal keyword:flying"), cards))
    assert "legalities" in sql
    assert "'legal'" in sql
    assert "'flying'" in sql
    assert " AND " in sql.upper()
