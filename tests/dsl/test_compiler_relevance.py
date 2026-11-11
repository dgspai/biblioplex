"""D11.1 — `compile_rank` extracts a relevance expression from the AST.

`compile_rank(ast, cards)` walks the parsed query and returns a
``ColumnElement`` evaluating ``word_similarity(<bare-terms>, name_search)``
when the query has at least one bare-word or unkeyed-quoted-phrase term
(plus ``name:`` KVs, which share the same name-substring code path).
Filter-only queries (e.g. ``t:legendary``) return ``None`` so the router
can fall back to a deterministic sort.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from biblioplex.dsl.compiler import compile_rank
from biblioplex.dsl.parser import parse


def cards_table() -> sa.Table:
    md = sa.MetaData()
    return sa.Table(
        "cards",
        md,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("name_search", sa.Text),
    )


def render(expr: sa.ColumnElement[float]) -> str:
    stmt = sa.select(expr.label("rank"))
    return str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def test_bare_word_produces_rank_with_word_similarity() -> None:
    cards = cards_table()
    rank = compile_rank(parse("invason"), cards)
    assert rank is not None
    sql = render(rank)
    assert "word_similarity" in sql
    assert "invason" in sql
    assert "name_search" in sql


def test_filter_only_query_has_no_rank() -> None:
    cards = cards_table()
    rank = compile_rank(parse("t:legendary"), cards)
    assert rank is None


def test_mixed_query_rank_uses_only_bare_terms() -> None:
    cards = cards_table()
    rank = compile_rank(parse("invason t:legendary"), cards)
    assert rank is not None
    sql = render(rank)
    assert "invason" in sql
    # The filter token must not leak into the rank expression.
    assert "legendary" not in sql


def test_name_kv_also_contributes_to_rank() -> None:
    cards = cards_table()
    rank = compile_rank(parse("name:bolt"), cards)
    assert rank is not None
    sql = render(rank)
    assert "bolt" in sql
    assert "word_similarity" in sql


def test_quoted_phrase_contributes_to_rank() -> None:
    cards = cards_table()
    rank = compile_rank(parse('"lightning bolt"'), cards)
    assert rank is not None
    sql = render(rank)
    assert "lightning bolt" in sql


def test_multiple_bare_words_join_into_rank() -> None:
    cards = cards_table()
    rank = compile_rank(parse("lightning bolt"), cards)
    assert rank is not None
    sql = render(rank)
    # Both terms appear in the joined query string passed to word_similarity.
    assert "lightning" in sql
    assert "bolt" in sql
