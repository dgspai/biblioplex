"""Tests for the AST → SQLAlchemy WHERE compiler (D3.3, §5).

We assert on the *compiled SQL text* rendered via `str(stmt.compile(...))`
with literal binds. That keeps the tests independent of an actual DB.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from biblioplex.dsl.compiler import compile_ast
from biblioplex.dsl.parser import parse


def cards_table() -> sa.Table:
    """Build a Core Table mirroring §3.1 column shape used by the compiler."""
    md = sa.MetaData()
    return sa.Table(
        "cards",
        md,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("oracle_id", sa.String),
        sa.Column("name", sa.Text),
        sa.Column("name_search", sa.Text),
        sa.Column("type_search", sa.Text),
        sa.Column("oracle_search", sa.Text),
        sa.Column("mana_cost_search", sa.Text),
        sa.Column("oracle_text", sa.Text),
        sa.Column("type_line", sa.Text),
        sa.Column("layout", sa.Text),
        sa.Column("rarity", sa.Text),
        sa.Column("cmc", sa.Numeric),
        sa.Column("edhrec_rank", sa.Integer),
        sa.Column("price_usd", sa.Numeric),
        sa.Column("power", sa.Text),
        sa.Column("toughness", sa.Text),
        sa.Column("loyalty", sa.Text),
        sa.Column("is_commander", sa.Boolean),
        sa.Column("colors_eff", postgresql.ARRAY(sa.Text)),
        sa.Column("color_identity", postgresql.ARRAY(sa.Text)),
        sa.Column("keywords", postgresql.ARRAY(sa.Text)),
        sa.Column("produced_mana", postgresql.ARRAY(sa.Text)),
        sa.Column("games_eff", postgresql.ARRAY(sa.Text)),
        sa.Column("legalities", postgresql.JSONB),
        sa.Column("raw", postgresql.JSONB),
    )


def render(expr: sa.ColumnElement[bool]) -> str:
    """Compile a WHERE fragment to its PG dialect SQL with literal binds."""
    stmt = sa.select(sa.literal(1)).where(expr)
    return str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


# ---- bare-word + key-value baselines ----


def test_bare_word_compiles_to_name_search_ilike() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("lightning"), cards))
    assert "name_search ILIKE" in sql
    assert "%lightning%" in sql
    # D11.1 — bare words also fuzzy-match via trgm word_similarity (`%>`).
    # psycopg paramstyle doubles `%` in rendered SQL, so the operator
    # surfaces as `%%>` in the compiled string.
    assert "name_search %%>" in sql
    assert " OR " in sql


def test_name_kv_compiles_to_name_search_ilike() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("name:bolt"), cards))
    assert "name_search ILIKE" in sql
    assert "%bolt%" in sql
    # D11.1 — name:X likewise picks up the fuzzy OR.
    assert "name_search %%>" in sql
    assert " OR " in sql


def test_oracle_kv_compiles_to_oracle_search_ilike() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("o:flying"), cards))
    assert "oracle_search ILIKE" in sql
    assert "%flying%" in sql


def test_type_kv_compiles_to_type_search_ilike() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("t:elf"), cards))
    assert "type_search ILIKE" in sql
    assert "%elf%" in sql


# ---- boolean composition (basic) ----


def test_and_combines_with_and() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("a b"), cards))
    assert " AND " in sql


def test_or_combines_with_or() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("a OR b"), cards))
    assert " OR " in sql


def test_negation_compiles_to_not() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("-a"), cards))
    assert "NOT " in sql


def test_exact_compiles_to_lower_eq() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("!Bolt"), cards))
    assert "lower(cards.name) = lower('Bolt')" in sql
