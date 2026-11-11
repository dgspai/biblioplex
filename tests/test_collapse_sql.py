"""Tests for the D1.9 cheapest-printing collapse SQL.

D1.5's collapse statement picks one row per `oracle_id` via `DISTINCT ON
(oracle_id) ORDER BY released_at ASC NULLS LAST` — the OLDEST printing
wins. D1.9 keeps that policy for `edhrec_rank` (per-oracle field,
identical across printings) but overrides it for `price_usd`: the row
that survives DISTINCT ON carries the MIN price across the oracle group,
via a `MIN(price_usd) OVER (PARTITION BY oracle_id)` window. Postgres
runs window functions before the DISTINCT ON dedup.

These string assertions stand in for a live-DB integration test (the
smoke step exercises real SQL).
"""

from __future__ import annotations

from biblioplex.ingest.collapse import COLLAPSE_SQL, PRINTINGS_SQL, STAGING_COLUMNS


def test_staging_columns_includes_new_d1_9_columns() -> None:
    """`edhrec_rank` and `price_usd` must appear in STAGING_COLUMNS before `raw`."""
    cols = list(STAGING_COLUMNS)
    assert "edhrec_rank" in cols
    assert "price_usd" in cols
    # Both must precede `raw` so the INSERT column order matches schema.sql
    # (where `raw` is the trailing jsonb-blob column).
    assert cols.index("edhrec_rank") < cols.index("raw")
    assert cols.index("price_usd") < cols.index("raw")


def test_collapse_sql_insert_includes_new_columns() -> None:
    """New columns appear in the INSERT column list AND ON CONFLICT UPDATE pairs."""
    sql = COLLAPSE_SQL
    # INSERT column list mentions both names.
    insert_clause = sql.split("INSERT INTO cards (", 1)[1].split(")", 1)[0]
    assert "edhrec_rank" in insert_clause
    assert "price_usd" in insert_clause
    # ON CONFLICT UPDATE refreshes both on a same-id reprint.
    update_clause = sql.split("ON CONFLICT (oracle_id) DO UPDATE SET", 1)[1]
    assert "edhrec_rank = EXCLUDED.edhrec_rank" in update_clause
    assert "price_usd = EXCLUDED.price_usd" in update_clause


def test_collapse_sql_contains_min_window_for_price_usd() -> None:
    """`price_usd` is replaced by a windowed MIN over the oracle partition."""
    sql = COLLAPSE_SQL
    assert "MIN(price_usd) OVER" in sql
    assert "PARTITION BY oracle_id" in sql
    assert "AS price_usd" in sql


def test_collapse_sql_edhrec_rank_not_aggregated() -> None:
    """`edhrec_rank` survives the DISTINCT ON pick as a bare column reference.

    It's per-oracle (identical across all printings of the same card), so
    the kept printing's value is already the right answer — no MIN/MAX
    window needed, and aggregating would be wasted work.
    """
    sql = COLLAPSE_SQL
    # No window or aggregate wraps edhrec_rank.
    assert "MIN(edhrec_rank)" not in sql
    assert "MAX(edhrec_rank)" not in sql
    # Bare column reference must appear in the SELECT projection.
    select_clause = sql.split("SELECT DISTINCT ON (oracle_id)", 1)[1].split(
        "FROM cards_staging", 1
    )[0]
    assert "edhrec_rank" in select_clause


def test_collapse_sql_preserves_distinct_on_ordering() -> None:
    """Adding the window function must not disturb the DISTINCT ON / ORDER BY pair."""
    sql = COLLAPSE_SQL
    assert "SELECT DISTINCT ON (oracle_id)" in sql
    assert "ORDER BY oracle_id, released_at ASC NULLS LAST" in sql


def test_collapse_sql_tiebreaks_on_id_after_released_at() -> None:
    """Bug.10: two printings sharing `released_at` must resolve deterministically.

    Without a secondary tiebreak the DISTINCT ON pick depends on the
    staging insertion order (Scryfall bulk stream order — not stable
    across refreshes), so `cards.id` for the same oracle can flip
    between refreshes and invalidate any client that cached the uuid.
    """
    sql = COLLAPSE_SQL
    assert "ORDER BY oracle_id, released_at ASC NULLS LAST, id ASC" in sql


# --- §5.1 games_eff cross-printing union ---------------------------------


def test_staging_columns_includes_games() -> None:
    """`games` (per-printing raw) lives in staging between colors_eff and raw."""
    cols = list(STAGING_COLUMNS)
    assert "games" in cols
    assert cols.index("games") > cols.index("colors_eff")
    assert cols.index("games") < cols.index("raw")


def test_collapse_sql_has_oracle_games_cte() -> None:
    """CTE pre-aggregates per-oracle games unions before the main SELECT."""
    sql = COLLAPSE_SQL
    assert "WITH oracle_games AS" in sql
    assert "array_agg(DISTINCT g)" in sql
    assert "unnest(coalesce(games, '{}'::text[]))" in sql
    assert "GROUP BY oracle_id" in sql


def test_collapse_sql_left_joins_oracle_games() -> None:
    """Main SELECT pulls the union via LEFT JOIN on oracle_id."""
    sql = COLLAPSE_SQL
    assert "LEFT JOIN oracle_games og USING (oracle_id)" in sql


def test_collapse_sql_projects_games_eff_from_cte() -> None:
    """Insert column is games_eff, projection sources from og.games_union."""
    sql = COLLAPSE_SQL
    # INSERT lists games_eff (the cards-table name), NOT games (staging name).
    insert_clause = sql.split("INSERT INTO cards (", 1)[1].split(")", 1)[0]
    assert "games_eff" in insert_clause
    assert ", games," not in insert_clause and "(games," not in insert_clause
    # Projection coalesces to empty text[] when no printing exposed games.
    assert "COALESCE(og.games_union, '{}'::text[]) AS games_eff" in sql
    # ON CONFLICT refreshes games_eff on a same-id reprint.
    update_clause = sql.split("ON CONFLICT (oracle_id) DO UPDATE SET", 1)[1]
    assert "games_eff = EXCLUDED.games_eff" in update_clause


def test_printings_sql_keeps_every_printing() -> None:
    """One row per printing id, refilled without TRUNCATE's exclusive lock."""
    assert PRINTINGS_SQL.lstrip().startswith("DELETE FROM printings;")
    assert "DISTINCT ON (id)" in PRINTINGS_SQL
    assert "FROM cards_staging" in PRINTINGS_SQL
    assert "TRUNCATE" not in PRINTINGS_SQL


def test_printings_sql_falls_back_to_first_face_image() -> None:
    """DFCs have no top-level image_uris; the first face's are used."""
    assert "COALESCE(raw->'image_uris', card_faces->0->'image_uris')" in PRINTINGS_SQL
