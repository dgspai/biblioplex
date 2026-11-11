"""Bug.27: `text(":X::TYPE")` never binds the `:X` param.

SQLAlchemy `text()` treats `::` as an escaped colon and skips the
preceding `:name` bindparam, so the compiled SQL still contains the
literal `:name::type` string. Postgres then hits `syntax error at or
near ":"`. The comment at `ingest/combos.py:_INSERT_SQL` already
documents this — Bug.27 backfills the fix to six sites that violated
it. Regression pins the bindparam parse on the *fixed* SQL literals.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

# Import each fixed SQL text so a future drift back to `:X::TYPE`
# fails the parse assertion below rather than silently syntax-erroring
# at runtime against a live Postgres.


def _bindparam_names(sql: str) -> set[str]:
    return {bp for bp in text(sql)._bindparams}


def test_bug27_combos_resolve_names_sql_binds_names() -> None:
    from biblioplex.ingest.combos import _RESOLVE_NAMES_SQL

    assert "names" in _bindparam_names(_RESOLVE_NAMES_SQL)
    assert ":names::text[]" not in _RESOLVE_NAMES_SQL


def test_bug27_decklist_resolve_and_fetch_sql_binds_all() -> None:
    from biblioplex.decklist.resolve import _RESOLVE_AND_FETCH_SQL

    binds = _bindparam_names(_RESOLVE_AND_FETCH_SQL)
    assert {"ords", "names", "lnames"}.issubset(binds)
    assert "::int[]" not in _RESOLVE_AND_FETCH_SQL
    assert "::text[]" not in _RESOLVE_AND_FETCH_SQL


@pytest.mark.parametrize(
    "sql,expected",
    [
        # decklist/resolve.py exact-match pass
        (
            "SELECT c.id, t.n AS input_name FROM cards c "
            "JOIN unnest(CAST(:names AS text[])) AS t(n) "
            "ON LOWER(c.name) = LOWER(t.n)",
            {"names"},
        ),
        # decklist/resolve.py fuzzy fallback
        (
            "SELECT m.n AS input_name, f.id "
            "FROM unnest(CAST(:misses AS text[])) AS m(n) "
            "CROSS JOIN LATERAL ( "
            "  SELECT c.id "
            "  FROM cards c "
            "  WHERE similarity(c.name_sort_key, LOWER(m.n)) >= :threshold "
            "  LIMIT 1 "
            ") f",
            {"misses", "threshold"},
        ),
        # routers/decks.py manabase nonbasic selector
        (
            "SELECT name, raw, color_identity, price_usd "
            "FROM cards "
            "WHERE name = ANY(CAST(:names AS text[])) "
            "AND color_identity <@ CAST(:ci AS text[]) "
            "ORDER BY array_position(CAST(:names AS text[]), name) "
            "LIMIT :nb_limit",
            {"names", "ci", "nb_limit"},
        ),
    ],
)
def test_bug27_call_site_sql_still_binds_params(sql: str, expected: set[str]) -> None:
    assert expected.issubset(_bindparam_names(sql))
    assert "::text[]" not in sql
    assert "::int[]" not in sql


def test_bug27_negative_pattern_broken_shape_still_broken() -> None:
    """Sanity: the pre-fix pattern really did skip the bindparam.

    Without this pin future refactors could reintroduce `:X::TYPE`
    thinking sqla handles it; asserting the negative case documents
    why the fixed shape uses `CAST()`.
    """
    assert "names" not in _bindparam_names("SELECT unnest(:names::text[])")
