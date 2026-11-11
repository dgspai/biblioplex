"""Staging → cards collapse per tech_spec.md §4.2 step 4.

After D1.1–D1.4 have streamed and inserted every kept card into
``cards_staging`` (which mirrors ``cards`` minus the generated columns and
``embedding``), this module renders the SQL that pivots staging into the
canonical ``cards`` table.

Pieces:

* :data:`STAGING_COLUMNS` — the authoritative ordered list of columns in
  ``cards_staging``. Must be kept in sync with ``schema.sql``.
* :data:`COLLAPSE_SQL` — pre-rendered ``INSERT … SELECT DISTINCT ON
  (oracle_id) … ORDER BY oracle_id, released_at ASC NULLS LAST … ON
  CONFLICT (oracle_id) DO UPDATE SET …``. ``DISTINCT ON (oracle_id)`` picks
  the oldest (first) printing per oracle; ``ON CONFLICT (oracle_id)``
  refreshes that oracle's row, adopting a new ``id`` when the chosen
  printing changed between refreshes (a same-date reprint with a lower id).
  Conflicting on ``id`` instead made such a refresh die on
  ``cards_oracle_id_key``. Nothing FKs ``cards.id``.

  D1.9: ``price_usd`` is NOT taken from the kept printing. The SELECT
  projection substitutes a windowed ``MIN(price_usd) OVER (PARTITION BY
  oracle_id)`` for the raw column so the kept row carries the cheapest
  printing's price across the whole oracle group. Postgres runs window
  functions before DISTINCT ON dedup, so this is well-defined.
  ``edhrec_rank`` is per-oracle (identical across printings) and stays a
  bare column reference — the DISTINCT ON pick already has the right value.

  §5.1: ``games`` (per-printing in staging) becomes ``games_eff`` (union
  across all printings of an oracle) in ``cards``. A ``WITH oracle_games``
  CTE pre-aggregates ``array_agg(DISTINCT g)`` over the unnested arrays per
  ``oracle_id``; the main SELECT ``LEFT JOIN``s it on ``oracle_id`` and
  projects ``COALESCE(og.games_union, '{}'::text[]) AS games_eff``. This
  fixes the drift where a paper-first-then-Arena card would have missed
  ``game:arena`` because the oldest printing's ``games`` was paper-only.
* :data:`TRUNCATE_STAGING_SQL` — §4.2 step 7 cleanup.

The refresh runner (D1.6) just executes these statements; no parameter
binding is needed because every value already lives in staging.
"""

from __future__ import annotations

# §4.2 step 3: same columns as `cards` minus generated cols and `embedding`.
# Order matters — the INSERT column list and the SELECT projection share it.
# D1.9 added `edhrec_rank` and `price_usd` before `raw` (matches schema.sql).
# §5.1 added `games` between `colors_eff` and `raw` — staging carries the raw
# per-printing array; collapse maps it to the unionized `games_eff` in cards.
STAGING_COLUMNS: tuple[str, ...] = (
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
)


def _cards_column(staging_column: str) -> str:
    """Map a STAGING_COLUMNS entry to its name in the ``cards`` table.

    Most columns are 1:1. ``games`` (per-printing in staging) maps to
    ``games_eff`` (union across printings) in ``cards`` per §5.1.
    """
    if staging_column == "games":
        return "games_eff"
    return staging_column


def _select_expr(column: str) -> str:
    """Return the SELECT-projection expression for one staging column.

    Most columns are bare references — the row that wins DISTINCT ON
    contributes its value verbatim. Two exceptions:

    * ``price_usd`` (D1.9): replaced by ``MIN(...) OVER (PARTITION BY
      oracle_id)`` so the kept printing carries the cheapest price across
      all printings of the same oracle (independent of which printing
      DISTINCT ON picks). Window functions run before DISTINCT ON dedup.
    * ``games`` (§5.1): sourced from the ``oracle_games`` CTE's union, not
      from the kept printing's per-printing array. Projected as
      ``games_eff`` (the cards-table name).
    """
    if column == "price_usd":
        return "MIN(price_usd) OVER (PARTITION BY oracle_id) AS price_usd"
    if column == "games":
        return "COALESCE(og.games_union, '{}'::text[]) AS games_eff"
    return column


def _render_collapse_sql(columns: tuple[str, ...]) -> str:
    """Build the collapse INSERT statement per §4.2 step 4.

    Threads three lists in lockstep:

    * INSERT column list — staging names mapped via :func:`_cards_column`
      (``games`` → ``games_eff``).
    * SELECT projection — same order, but ``price_usd`` is the windowed MIN
      and ``games`` is the CTE-sourced union (see :func:`_select_expr`).
    * ON CONFLICT UPDATE pairs — follow the INSERT names so EXCLUDED
      carries post-aggregation values.

    The ``oracle_games`` CTE pre-aggregates per-oracle ``games`` unions
    once, then the main SELECT ``LEFT JOIN``s it on ``oracle_id``. USING
    keeps ``oracle_id`` unqualified in the main SELECT so existing test
    assertions on ``DISTINCT ON (oracle_id)`` and ``ORDER BY oracle_id``
    keep working.
    """
    insert_cols = ", ".join(_cards_column(c) for c in columns)
    select_list = ", ".join(_select_expr(c) for c in columns)
    update_pairs = ", ".join(
        f"{_cards_column(c)} = EXCLUDED.{_cards_column(c)}"
        for c in columns
        if c != "oracle_id"
    )
    return (
        "WITH oracle_games AS (\n"  # noqa: S608
        "  SELECT oracle_id, array_agg(DISTINCT g) AS games_union\n"
        "  FROM cards_staging, unnest(coalesce(games, '{}'::text[])) AS g\n"
        "  GROUP BY oracle_id\n"
        ")\n"
        f"INSERT INTO cards ({insert_cols})\n"
        f"SELECT DISTINCT ON (oracle_id) {select_list}\n"
        "FROM cards_staging\n"
        "LEFT JOIN oracle_games og USING (oracle_id)\n"
        "ORDER BY oracle_id, released_at ASC NULLS LAST, id ASC\n"
        f"ON CONFLICT (oracle_id) DO UPDATE SET {update_pairs};"
    )


COLLAPSE_SQL: str = _render_collapse_sql(STAGING_COLUMNS)

# Every printing staging saw, for the card-detail printings strip. DELETE,
# not TRUNCATE: TRUNCATE's ACCESS EXCLUSIVE lock blocks readers (see tags.py).
PRINTINGS_SQL: str = """
DELETE FROM printings;
INSERT INTO printings (id, oracle_id, set_code, set_name, collector_number,
    released_at, rarity, finishes, image_small, image_normal, usd, usd_foil)
SELECT DISTINCT ON (id)
    id, oracle_id, raw->>'set', raw->>'set_name', raw->>'collector_number',
    released_at, rarity,
    ARRAY(SELECT jsonb_array_elements_text(COALESCE(raw->'finishes', '[]'))),
    img->>'small', img->>'normal',
    (raw->'prices'->>'usd')::numeric, (raw->'prices'->>'usd_foil')::numeric
FROM cards_staging,
    LATERAL (SELECT COALESCE(raw->'image_uris', card_faces->0->'image_uris') AS img) i
ORDER BY id;
"""

# §4.2 step 7. Plain TRUNCATE — no CASCADE needed (nothing FKs into staging).
TRUNCATE_STAGING_SQL: str = "TRUNCATE TABLE cards_staging;"
