"""Inventory service for card resolution and database persistence per D18.2."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import text

from biblioplex.inventory.deckbox import ParsedDeckboxItem


class _Connection(Protocol):
    """Protocol matching AsyncConnection / Connection subset used."""

    async def execute(self, statement: Any, parameters: Any = None) -> Any: ...


def _squash_ws(name: str) -> str:
    """Drop every whitespace character. `"a . . ."` and `"a..."` collapse to
    the same key, which is how Deckbox and Scryfall punctuation differences
    are reconciled without a per-punctuation rule."""
    return "".join(name.split())


async def resolve_card_names(
    conn: _Connection,
    items: Sequence[ParsedDeckboxItem],
) -> dict[str, tuple[UUID, str]]:
    """Map each item's lowercased name to ``(oracle_id, canonical_name)``.

    Three passes, each running only on what the previous one missed:

    1. Batched exact match on ``lower(name)`` — resolves ~100%.
    2. Front-face match for split names (``Fire // Ice`` -> ``Fire``).
    3. Whitespace-insensitive match. Deckbox and Scryfall disagree on
       spacing inside punctuation runs — Scryfall writes
       ``With Great Power . . .``, Deckbox writes ``With Great Power...``.
       Squashing all whitespace on both sides matches them without
       hard-coding an ellipsis rule. Unindexable (functional expression
       over 35k rows), which is why it is last and gated on leftovers.

    Shared by the inventory and wishlist syncs — both consume the same
    Deckbox CSV shape, so name resolution has exactly one implementation.
    """
    lower_names = [n.lower() for n in {item.card_name for item in items}]
    if not lower_names:
        return {}

    result = await conn.execute(
        text(
            "SELECT oracle_id, name, lower(name) AS lower_name "
            "FROM cards "
            "WHERE lower(name) = ANY(:names)"
        ),
        {"names": lower_names},
    )
    name_to_oracle: dict[str, tuple[UUID, str]] = {
        r["lower_name"]: (r["oracle_id"], r["name"]) for r in result.mappings().all()
    }

    missing_lower = [name for name in lower_names if name not in name_to_oracle]
    split_fronts = [
        name.split(" // ")[0] for name in missing_lower if " // " in name
    ]
    if split_fronts:
        split_res = await conn.execute(
            text(
                "SELECT oracle_id, name, lower(name) AS lower_name "
                "FROM cards "
                "WHERE lower(name) = ANY(:fronts)"
            ),
            {"fronts": split_fronts},
        )
        for r in split_res.mappings().all():
            for orig_name in missing_lower:
                if orig_name.startswith(r["lower_name"]):
                    name_to_oracle[orig_name] = (r["oracle_id"], r["name"])

    still_missing = [n for n in lower_names if n not in name_to_oracle]
    if still_missing:
        squashed_to_input = {_squash_ws(n): n for n in still_missing}
        squash_res = await conn.execute(
            text(
                "SELECT oracle_id, name, "
                "       regexp_replace(lower(name), '\\s+', '', 'g') AS squashed "
                "FROM cards "
                "WHERE regexp_replace(lower(name), '\\s+', '', 'g') = ANY(:squashed)"
            ),
            {"squashed": list(squashed_to_input)},
        )
        for r in squash_res.mappings().all():
            orig = squashed_to_input.get(r["squashed"])
            if orig is not None:
                name_to_oracle[orig] = (r["oracle_id"], r["name"])

    return name_to_oracle


async def resolve_and_sync_inventory(
    conn: _Connection,
    items: Sequence[ParsedDeckboxItem],
    mode: str = "replace",
    source: str = "deckbox",
    deckbox_url: str | None = None,
) -> dict[str, Any]:
    """Resolve card names to oracle_ids and persist inventory in Postgres.

    Mode:
      - "replace": clear existing inventory for source before inserting.
      - "merge": update existing quantities or insert new rows.
    """
    if not items:
        if mode == "replace":
            await conn.execute(
                text("DELETE FROM inventory WHERE source = :source"),
                {"source": source},
            )
        now_str = datetime.now(UTC).isoformat()
        return {
            "object": "inventory_sync",
            "mode": mode,
            "total_cards": 0,
            "distinct_cards": 0,
            "unmatched": [],
            "synced_at": now_str,
        }

    name_to_oracle = await resolve_card_names(conn, items)

    resolved_records: list[dict[str, Any]] = []
    unmatched: list[str] = []

    for item in items:
        low = item.card_name.lower()
        if low in name_to_oracle:
            oid, canonical_name = name_to_oracle[low]
            resolved_records.append(
                {
                    "oracle_id": str(oid),
                    "quantity": item.quantity,
                    "foil_quantity": item.foil_quantity,
                    "card_name": canonical_name,
                    "set_name": item.set_name,
                    "set_code": item.set_code,
                    "collector_number": item.collector_number,
                    "scryfall_id": item.scryfall_id,
                    "source": source,
                    "source_price_usd": item.price_usd,
                }
            )
        else:
            unmatched.append(item.card_name)

    if mode == "replace":
        await conn.execute(
            text("DELETE FROM inventory WHERE source = :source"),
            {"source": source},
        )
    elif resolved_records:
        # `merge` = per-card snapshot overwrite: clear only the oracle_ids
        # present in this payload, then re-insert them. Without the delete,
        # a repeated merge sync would stack duplicate rows (there is no
        # unique key on `inventory` — one card legitimately has one row per
        # printing/edition).
        await conn.execute(
            text(
                "DELETE FROM inventory WHERE source = :source "
                "AND oracle_id = ANY(CAST(:oracle_ids AS uuid[]))"
            ),
            {
                "source": source,
                "oracle_ids": sorted({r["oracle_id"] for r in resolved_records}),
            },
        )

    if resolved_records:
        # executemany — a 10k-card collection is one round-trip's worth of
        # protocol work instead of 10k. Bug.27: `CAST(:x AS uuid)` not
        # `:x::uuid` — SQLAlchemy `text()` reads `::` as an escaped colon.
        insert_sql = text(
            "INSERT INTO inventory "
            "(oracle_id, quantity, foil_quantity, card_name, set_code, set_name, "
            " collector_number, scryfall_id, source, source_price_usd) "
            "VALUES "
            "(CAST(:oracle_id AS uuid), :quantity, :foil_quantity, "
            ":card_name, :set_code, :set_name, :collector_number, "
            "CAST(:scryfall_id AS uuid), :source, :source_price_usd)"
        )
        await conn.execute(insert_sql, resolved_records)

    total_qty = sum(r["quantity"] for r in resolved_records)
    distinct_cards = len({r["oracle_id"] for r in resolved_records})
    now_dt = datetime.now(UTC)
    now_str = now_dt.isoformat()

    settings_sql = text(
        "INSERT INTO inventory_sync_settings "
        "(id, deckbox_url, last_synced_at, last_status, last_card_count) "
        "VALUES (1, :url, :synced_at, 'ok', :card_count) "
        "ON CONFLICT (id) DO UPDATE SET "
        "deckbox_url = COALESCE(:url, inventory_sync_settings.deckbox_url), "
        "last_synced_at = :synced_at, "
        "last_status = 'ok', "
        "last_card_count = :card_count"
    )
    await conn.execute(
        settings_sql,
        {"url": deckbox_url, "synced_at": now_dt, "card_count": total_qty},
    )

    return {
        "object": "inventory_sync",
        "mode": mode,
        "total_cards": total_qty,
        "distinct_cards": distinct_cards,
        "unmatched": unmatched,
        "synced_at": now_str,
    }


async def get_inventory_summary(conn: _Connection) -> dict[str, Any]:
    """Get aggregate inventory summary metrics."""
    res = await conn.execute(
        text(
            "SELECT "
            "  COALESCE(SUM(i.quantity), 0) AS total_cards, "
            "  COUNT(DISTINCT i.oracle_id) AS distinct_cards, "
            "  COALESCE(SUM(i.quantity * COALESCE(i.source_price_usd, c.price_usd)), 0.0) "
            "    AS total_value_usd "
            "FROM inventory i "
            "LEFT JOIN cards c ON c.oracle_id = i.oracle_id"
        )
    )
    row = res.mappings().one()

    settings_res = await conn.execute(
        text(
            "SELECT deckbox_url, last_synced_at "
            "FROM inventory_sync_settings WHERE id = 1"
        )
    )
    s_row = settings_res.mappings().one_or_none()

    synced_at = (
        s_row["last_synced_at"].isoformat()
        if s_row and s_row["last_synced_at"]
        else None
    )
    deckbox_url = s_row["deckbox_url"] if s_row else None

    return {
        "object": "inventory_summary",
        "total_cards": int(row["total_cards"]),
        "distinct_cards": int(row["distinct_cards"]),
        "total_value_usd": round(float(row["total_value_usd"]), 2),
        "last_synced_at": synced_at,
        "deckbox_url": deckbox_url,
    }


async def list_inventory(
    conn: _Connection,
    q: str | None = None,
    page: int = 1,
    limit: int = 50,
) -> dict[str, Any]:
    """List inventory items with search filter and pagination."""
    offset = (max(1, page) - 1) * limit
    where_clause = ""
    params: dict[str, Any] = {"limit": limit, "offset": offset}

    if q and q.strip():
        where_clause = "WHERE i.card_name ILIKE :q "
        params["q"] = f"%{q.strip()}%"

    count_sql = f"SELECT COUNT(*) AS total FROM inventory i {where_clause}"  # noqa: S608
    count_res = await conn.execute(text(count_sql), params)
    total = int(count_res.mappings().one()["total"])

    select_sql = (
        "SELECT i.id, i.oracle_id, i.card_name, i.quantity, i.foil_quantity, "  # noqa: S608
        "       i.set_name, i.collector_number, i.updated_at, c.raw, c.type_line, "
        "       COALESCE(i.source_price_usd, c.price_usd) AS price_usd, "
        "       p.id AS printing_id, p.image_normal AS printing_image, "
        "       c.predicted_labels "
        "FROM inventory i "
        "LEFT JOIN cards c ON c.oracle_id = i.oracle_id "
        "LEFT JOIN printings p ON p.id = i.scryfall_id "
        f"{where_clause}"
        "ORDER BY i.card_name ASC "
        "LIMIT :limit OFFSET :offset"
    )
    res = await conn.execute(text(select_sql), params)
    rows = res.mappings().all()

    data: list[dict[str, Any]] = []
    for r in rows:
        item = {
            "id": str(r["id"]),
            "oracle_id": str(r["oracle_id"]),
            "card_name": r["card_name"],
            "quantity": r["quantity"],
            "foil_quantity": r["foil_quantity"],
            "set_name": r["set_name"],
            "collector_number": r["collector_number"],
            "price_usd": float(r["price_usd"]) if r["price_usd"] is not None else None,
            "type_line": r["type_line"],
            "predicted_labels": list(r["predicted_labels"] or []),
            "card_raw": r["raw"],
            "printing": None if r["printing_id"] is None else {
                "id": str(r["printing_id"]),
                "image_uris": {"normal": r["printing_image"]},
            },
        }
        data.append(item)

    return {
        "object": "list",
        "data": data,
        "total_cards": total,
        "page": page,
        "limit": limit,
        "has_more": (offset + len(data)) < total,
    }


async def update_card_quantity(
    conn: _Connection,
    oracle_id: str,
    quantity: int,
    foil_quantity: int = 0,
) -> dict[str, Any] | None:
    """Set one card's owned quantity manually (D18.3).

    ``quantity == 0 and foil_quantity == 0`` deletes the manual row.
    Returns ``None`` when ``oracle_id`` is not a known card, so the caller
    can 404 instead of writing an orphan row the FK would reject anyway.
    """
    name_res = await conn.execute(
        text("SELECT name FROM cards WHERE oracle_id = CAST(:oid AS uuid) LIMIT 1"),
        {"oid": oracle_id},
    )
    name_row = name_res.mappings().one_or_none()
    if name_row is None:
        return None

    # Manual rows are keyed by oracle_id alone (no printing granularity),
    # so delete-then-insert is the whole upsert.
    await conn.execute(
        text(
            "DELETE FROM inventory "
            "WHERE oracle_id = CAST(:oid AS uuid) AND source = 'manual'"
        ),
        {"oid": oracle_id},
    )
    if quantity > 0 or foil_quantity > 0:
        await conn.execute(
            text(
                "INSERT INTO inventory "
                "(oracle_id, quantity, foil_quantity, card_name, source) "
                "VALUES (CAST(:oid AS uuid), :quantity, :foil_quantity, "
                ":card_name, 'manual')"
            ),
            {
                "oid": oracle_id,
                "quantity": quantity,
                "foil_quantity": foil_quantity,
                "card_name": name_row["name"],
            },
        )

    return {
        "object": "inventory_card",
        "oracle_id": oracle_id,
        "card_name": name_row["name"],
        "quantity": quantity,
        "foil_quantity": foil_quantity,
        "source": "manual",
    }


async def clear_inventory(conn: _Connection) -> None:
    """Clear all items from inventory table and reset settings."""
    await conn.execute(text("DELETE FROM inventory"))
    await conn.execute(text("DELETE FROM inventory_sync_settings"))
