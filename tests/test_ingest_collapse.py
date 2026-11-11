"""Tests for staging → cards collapse (D1.5, tech_spec.md §4.2 step 4).

Real-DB coverage (the round-trip insert-staging → collapse → assert oldest
survives) is skipped here: no live Postgres or pytest-postgresql fixture is
available in the unit-test environment. The integration assertion is
covered by the M8 smoke test against the compose stack.

What we DO test here:
* SQL string shape — the rendered collapse statement matches the spec's
  template (`DISTINCT ON (oracle_id)`, `ORDER BY oracle_id, released_at
  ASC NULLS LAST`, `ON CONFLICT (oracle_id) DO UPDATE SET`).
* The set of columns inserted matches `STAGING_COLUMNS` exactly — keeps
  staging and `cards` schema in lockstep.
* `TRUNCATE cards_staging` is what the post-collapse cleanup runs (§4.2
  step 7).
"""

from __future__ import annotations

import pytest

from biblioplex.ingest.collapse import (
    COLLAPSE_SQL,
    STAGING_COLUMNS,
    TRUNCATE_STAGING_SQL,
    _cards_column,
)


def test_staging_columns_match_schema() -> None:
    """The Python COLUMN list mirrors what schema.sql declares for cards_staging."""
    # If you add/remove a column in schema.sql cards_staging, update this list
    # and STAGING_COLUMNS together — the collapse INSERT depends on it.
    expected = [
        "id",
        "oracle_id",
        "name",
        "type_line",
        "oracle_text",
        "mana_cost",
        "cmc",
        "colors",
        "color_identity",
        "keywords",
        "produced_mana",
        "power",
        "toughness",
        "loyalty",
        "defense",
        "layout",
        "rarity",
        "legalities",
        "card_faces",
        "all_parts",
        "released_at",
        "edhrec_rank",
        "price_usd",
        "name_search",
        "type_search",
        "oracle_search",
        "mana_cost_search",
        "colors_eff",
        "games",
        "color_sort_key",
        "raw",
    ]
    assert list(STAGING_COLUMNS) == expected


def test_collapse_sql_shape() -> None:
    """Rendered SQL contains the spec-mandated clauses."""
    sql = COLLAPSE_SQL
    assert "INSERT INTO cards" in sql
    assert "SELECT DISTINCT ON (oracle_id)" in sql
    assert "FROM cards_staging" in sql
    assert "ORDER BY oracle_id, released_at ASC NULLS LAST" in sql
    assert "ON CONFLICT (oracle_id) DO UPDATE SET" in sql


def test_collapse_sql_lists_every_staging_column() -> None:
    """Every staging column appears in the INSERT column list AND the SELECT.

    Catches drift between STAGING_COLUMNS and the rendered SQL — if someone
    edits one but not the other, this fails.
    """
    sql = COLLAPSE_SQL
    for col in STAGING_COLUMNS:
        assert col in sql, f"column {col!r} missing from collapse SQL"


def test_collapse_sql_updates_all_columns_except_oracle_id() -> None:
    """ON CONFLICT (oracle_id) DO UPDATE SET must refresh every other column.

    `cards` holds one row per oracle, so `oracle_id` is the conflict target.
    `id` IS in the SET: when Scryfall's earliest printing changes (Ajani
    Resolute gained a same-date printing with a lower id), the row adopts
    the new representative instead of the insert dying on
    `cards_oracle_id_key`. Nothing FKs `cards.id`; dependents key on
    `oracle_id`.
    """
    sql = COLLAPSE_SQL
    update_clause = sql.split("ON CONFLICT (oracle_id) DO UPDATE SET", 1)[1]
    assert "id = EXCLUDED.id" in update_clause
    for col in STAGING_COLUMNS:
        if col == "oracle_id":
            continue
        # §5.1: staging `games` → cards `games_eff`; other cols 1:1.
        cards_col = _cards_column(col)
        assert f"{cards_col} = EXCLUDED.{cards_col}" in update_clause, (
            f"column {cards_col!r} not refreshed in ON CONFLICT SET clause"
        )
    assert "oracle_id = EXCLUDED.oracle_id" not in update_clause


def test_truncate_staging_sql_shape() -> None:
    """Step 7 cleanup truncates `cards_staging`."""
    assert "TRUNCATE" in TRUNCATE_STAGING_SQL
    assert "cards_staging" in TRUNCATE_STAGING_SQL


@pytest.mark.skip(
    reason="needs live pgvector db — covered by smoke test in M8"
)
def test_collapse_keeps_oldest_per_oracle_id() -> None:
    """Integration: two staging rows with same oracle_id; older released_at wins.

    Skipped — requires a real Postgres instance with the cards / cards_staging
    schema. Re-enable when pytest-postgresql or a docker-compose harness is
    wired up (planned for M8).
    """
