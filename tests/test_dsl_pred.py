"""``pred:<label>`` DSL clause — template-classifier-plan.md Step 5.

Parallel to ``otag:``: matches when the label scored >= 0.5 in
``classify_by_rules`` and got written to ``cards.predicted_labels``.
Compiles to ``cards.predicted_labels @> ARRAY[:label]`` (GIN-backed).

Unknown labels surface as ``UnsupportedOperatorError`` (→ 422 at the
HTTP layer) instead of silently returning an empty result set.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from biblioplex.classify.rules import LABELS
from biblioplex.dsl.compiler import UnsupportedOperatorError, compile_ast
from biblioplex.dsl.parser import parse


def _cards() -> sa.Table:
    md = sa.MetaData()
    return sa.Table(
        "cards",
        md,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("name", sa.Text),
        sa.Column("name_search", sa.Text),
        sa.Column("predicted_labels", postgresql.ARRAY(sa.Text)),
    )


def _render(expr: sa.ColumnElement[bool]) -> str:
    return str(
        expr.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()


@pytest.mark.parametrize("label", list(LABELS))
def test_pred_compiles_to_array_contains(label: str) -> None:
    """``pred:X`` compiles to ``predicted_labels @> ARRAY[X]``."""
    expr = compile_ast(parse(f"pred:{label}"), _cards())
    sql = _render(expr)
    assert "predicted_labels" in sql
    assert "@>" in sql
    assert label in sql


def test_pred_unknown_label_raises() -> None:
    """Typo / unknown label → 422 instead of silent empty result."""
    with pytest.raises(UnsupportedOperatorError) as exc:
        compile_ast(parse("pred:rmap"), _cards())
    assert "rmap" in str(exc.value)


def test_pred_is_case_insensitive() -> None:
    """``pred:RAMP`` lowercases to match LABELS membership."""
    expr = compile_ast(parse("pred:RAMP"), _cards())
    sql = _render(expr)
    assert "ramp" in sql


def test_pred_with_non_colon_op_rejected() -> None:
    """Only ``pred:X`` shape — `>`/`>=` make no sense for a set membership."""
    with pytest.raises(UnsupportedOperatorError):
        compile_ast(parse("pred>=ramp"), _cards())
