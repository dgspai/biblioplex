"""``land:<facet>`` — filter by the persisted land profile."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from biblioplex.dsl.compiler import UnsupportedOperatorError, compile_ast
from biblioplex.dsl.parser import parse


def _sql(q: str) -> str:
    cards = sa.Table(
        "cards", sa.MetaData(),
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("name", sa.Text), sa.Column("name_search", sa.Text),
        sa.Column("land_profile", postgresql.JSONB),
    )
    expr = compile_ast(parse(q), cards)
    return str(expr.compile(dialect=postgresql.dialect(),
                            compile_kwargs={"literal_binds": True})).lower()


def test_colour_bucket() -> None:
    assert "land_profile ->> 'colors') = 'dual'" in _sql("land:dual")


def test_etb_bucket() -> None:
    assert "land_profile ->> 'etb') = 'untapped'" in _sql("land:untapped")


def test_utility_and_plain() -> None:
    assert "jsonb_array_length" in _sql("land:utility") and "> 0" in _sql("land:utility")
    assert "= 0" in _sql("land:plain")


def test_utility_kind() -> None:
    assert "land_profile -> 'utility') ? 'creature_land'" in _sql("land:creature_land")


def test_unknown_value_is_rejected() -> None:
    with pytest.raises(UnsupportedOperatorError):
        _sql("land:quad")
