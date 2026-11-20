"""Core ``Table`` for ``cards`` that the DSL compiler builds WHERE clauses against."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


def _build() -> sa.Table:
    """Construct a Core ``Table`` matching the §3.1 ``cards`` shape.

    The DSL compiler (``compile_ast``) reads ``Table.c.<col>`` to assemble
    its WHERE expression. The router only ever issues SELECTs through the
    rendered SQL, so we don't need DDL fidelity — just every column the
    compiler references plus the columns we sort/select on.

    Built once at import time and reused per-request — building a fresh
    MetaData on every search would be wasteful and prevents column-element
    identity comparisons that SA uses internally for clause caching.
    """
    md = sa.MetaData()
    return sa.Table(
        "cards",
        md,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("oracle_id", sa.String),
        sa.Column("name", sa.Text),
        sa.Column("name_search", sa.Text),
        sa.Column("name_sort_key", sa.Text),
        sa.Column("type_search", sa.Text),
        sa.Column("type_line", sa.Text),
        sa.Column("oracle_search", sa.Text),
        sa.Column("oracle_text", sa.Text),
        sa.Column("mana_cost_search", sa.Text),
        sa.Column("mana_cost", sa.Text),
        sa.Column("layout", sa.Text),
        sa.Column("rarity", sa.Text),
        sa.Column("cmc", sa.Numeric),
        sa.Column("power", sa.Text),
        sa.Column("toughness", sa.Text),
        sa.Column("loyalty", sa.Text),
        sa.Column("released_at", sa.Date),
        sa.Column("is_commander", sa.Boolean),
        # D1.9 — populated at ingest. Wired into ``order=edhrec|usd`` (D4.6);
        # both indexed NULLS LAST in schema.sql so unranked/unpriced cards sink.
        sa.Column("edhrec_rank", sa.Integer),
        sa.Column("price_usd", sa.Numeric),
        sa.Column("colors_eff", postgresql.ARRAY(sa.Text)),
        # §6.1: WUBRG-cycle position (0–31) for `order=color`.
        sa.Column("color_sort_key", sa.Integer),
        sa.Column("color_identity", postgresql.ARRAY(sa.Text)),
        sa.Column("keywords", postgresql.ARRAY(sa.Text)),
        sa.Column("produced_mana", postgresql.ARRAY(sa.Text)),
        # §5.1: per-oracle union of `games` across printings; backs `game:`.
        sa.Column("games_eff", postgresql.ARRAY(sa.Text)),
        sa.Column("legalities", postgresql.JSONB),
        # template-classifier-plan.md Step 5 — rules-layer classifier
        # output. `predicted_labels` backs `pred:<label>` (GIN-indexed
        # `@>` lookup); `label_scores` is read-only payload for UI display.
        sa.Column("predicted_labels", postgresql.ARRAY(sa.Text)),
        sa.Column("label_scores", postgresql.JSONB),
        sa.Column("label_reasons", postgresql.JSONB),
        sa.Column("land_profile", postgresql.JSONB),
        sa.Column("raw", postgresql.JSONB),
    )


CARDS = _build()
