"""Wishlist persistence per D18.8.

A Deckbox wishlist is just another Deckbox set, so the CSV fetch and parse
in :mod:`biblioplex.inventory.deckbox` and the name resolution in
:mod:`biblioplex.inventory.service` are reused verbatim. Only storage and
the summary shape differ.

Deliberately a separate `wishlist` TABLE rather than a `list_type` column on
`inventory`: every ownership query (DSL `is:owned`, deck coverage,
`/inventory/oracle-ids`) would otherwise need an `AND list_type = 'owned'`
filter, and forgetting it counts wanted cards as owned — silently, with no
error to notice.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from biblioplex.inventory.deckbox import ParsedDeckboxItem
from biblioplex.inventory.service import _Connection, resolve_card_names


async def sync_wishlist(
    conn: _Connection,
    items: Sequence[ParsedDeckboxItem],
    mode: str = "replace",
    source: str = "deckbox",
) -> dict[str, Any]:
    """Resolve names and persist the wishlist.

    Mode:
      - ``replace``: clear every row for ``source`` before inserting.
      - ``merge``: clear only the incoming oracle ids, then insert. Without
        that delete a repeated merge would stack duplicate rows — there is
        no unique key on `wishlist` (same rationale as `inventory`: one row
        per printing).
    """
    if not items:
        if mode == "replace":
            await conn.execute(
                text("DELETE FROM wishlist WHERE source = :source"),
                {"source": source},
            )
        return {
            "object": "wishlist_sync",
            "mode": mode,
            "total_cards": 0,
            "distinct_cards": 0,
            "unmatched": [],
            "synced_at": datetime.now(UTC).isoformat(),
        }

    name_to_oracle = await resolve_card_names(conn, items)

    resolved_records: list[dict[str, Any]] = []
    unmatched: list[str] = []
    for item in items:
        hit = name_to_oracle.get(item.card_name.lower())
        if hit is None:
            unmatched.append(item.card_name)
            continue
        oid, canonical_name = hit
        resolved_records.append(
            {
                "oracle_id": str(oid),
                "quantity": item.quantity,
                "card_name": canonical_name,
                "set_name": item.set_name,
                "set_code": item.set_code,
                "source": source,
                "source_price_usd": item.price_usd,
            }
        )

    if mode == "replace":
        await conn.execute(
            text("DELETE FROM wishlist WHERE source = :source"),
            {"source": source},
        )
    elif resolved_records:
        await conn.execute(
            text(
                "DELETE FROM wishlist WHERE source = :source "
                "AND oracle_id = ANY(CAST(:oracle_ids AS uuid[]))"
            ),
            {
                "source": source,
                "oracle_ids": sorted({r["oracle_id"] for r in resolved_records}),
            },
        )

    if resolved_records:
        # Bug.27: `CAST(:x AS uuid)`, never `:x::uuid` — SQLAlchemy `text()`
        # reads `::` as an escaped colon. One executemany, not a loop.
        await conn.execute(
            text(
                "INSERT INTO wishlist "
                "(oracle_id, quantity, card_name, set_code, set_name, source, "
                " source_price_usd) "
                "VALUES (CAST(:oracle_id AS uuid), :quantity, "
                ":card_name, :set_code, :set_name, :source, :source_price_usd)"
            ),
            resolved_records,
        )

    return {
        "object": "wishlist_sync",
        "mode": mode,
        "total_cards": sum(r["quantity"] for r in resolved_records),
        "distinct_cards": len({r["oracle_id"] for r in resolved_records}),
        "unmatched": unmatched,
        "synced_at": datetime.now(UTC).isoformat(),
    }


async def get_wishlist_summary(conn: _Connection) -> dict[str, Any]:
    """Aggregate wishlist metrics.

    ``last_synced_at`` is ``max(updated_at)`` over the rows themselves — the
    wishlist needs no companion settings table because the data already
    carries its own timestamp.

    ``owned_distinct_cards`` counts wishlist entries the user already owns,
    which is the "you can take these off the list" number.
    """
    row = (
        await conn.execute(
            text(
                "SELECT "
                "  COALESCE(SUM(w.quantity), 0)      AS total_cards, "
                "  COUNT(DISTINCT w.oracle_id)       AS distinct_cards, "
                "  COALESCE(SUM(w.quantity * COALESCE(w.source_price_usd, c.price_usd)), 0.0) "
                "    AS total_value_usd, "
                "  MAX(w.updated_at)                 AS last_synced_at, "
                "  COUNT(DISTINCT i.oracle_id)       AS owned_distinct_cards "
                "FROM wishlist w "
                "LEFT JOIN cards c ON c.oracle_id = w.oracle_id "
                "LEFT JOIN inventory i ON i.oracle_id = w.oracle_id AND i.quantity > 0"
            )
        )
    ).mappings().one()

    synced = row["last_synced_at"]
    return {
        "object": "wishlist_summary",
        "total_cards": int(row["total_cards"]),
        "distinct_cards": int(row["distinct_cards"]),
        "total_value_usd": round(float(row["total_value_usd"]), 2),
        "owned_distinct_cards": int(row["owned_distinct_cards"]),
        "last_synced_at": synced.isoformat() if synced else None,
    }


async def list_wishlist(
    conn: _Connection,
    q: str | None = None,
    page: int = 1,
    limit: int = 50,
) -> dict[str, Any]:
    """Paginated wishlist listing. Each row carries `owned_quantity` so the
    UI can strike through entries the user has since acquired."""
    offset = (max(1, page) - 1) * limit
    where_clause = ""
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if q and q.strip():
        where_clause = "WHERE w.card_name ILIKE :q "
        params["q"] = f"%{q.strip()}%"

    total = int(
        (
            await conn.execute(
                text(f"SELECT COUNT(*) AS total FROM wishlist w {where_clause}"),  # noqa: S608
                params,
            )
        )
        .mappings()
        .one()["total"]
    )

    rows = (
        await conn.execute(
            text(
                "SELECT w.id, w.oracle_id, w.card_name, w.quantity, w.set_name, "  # noqa: S608
                "       c.raw, c.type_line, "
                "       COALESCE(w.source_price_usd, c.price_usd) AS price_usd, "
                "       COALESCE(("
                "         SELECT SUM(i.quantity) FROM inventory i "
                "         WHERE i.oracle_id = w.oracle_id"
                "       ), 0) AS owned_quantity "
                "FROM wishlist w "
                "LEFT JOIN cards c ON c.oracle_id = w.oracle_id "
                f"{where_clause}"
                "ORDER BY w.card_name ASC "
                "LIMIT :limit OFFSET :offset"
            ),
            params,
        )
    ).mappings().all()

    data = [
        {
            "id": str(r["id"]),
            "oracle_id": str(r["oracle_id"]),
            "card_name": r["card_name"],
            "quantity": r["quantity"],
            "owned_quantity": int(r["owned_quantity"]),
            "set_name": r["set_name"],
            "price_usd": float(r["price_usd"]) if r["price_usd"] is not None else None,
            "type_line": r["type_line"],
            "card_raw": r["raw"],
        }
        for r in rows
    ]

    return {
        "object": "list",
        "data": data,
        "total_cards": total,
        "page": page,
        "limit": limit,
        "has_more": (offset + len(data)) < total,
    }


async def update_wishlist_quantity(
    conn: _Connection,
    oracle_id: str,
    quantity: int,
) -> dict[str, Any] | None:
    """Set one card's wanted quantity. ``quantity == 0`` removes the row.

    Returns ``None`` for an unknown oracle so the caller can 404 rather than
    writing a row the FK would reject anyway.
    """
    name_row = (
        await conn.execute(
            text("SELECT name FROM cards WHERE oracle_id = CAST(:oid AS uuid) LIMIT 1"),
            {"oid": oracle_id},
        )
    ).mappings().one_or_none()
    if name_row is None:
        return None

    await conn.execute(
        text(
            "DELETE FROM wishlist "
            "WHERE oracle_id = CAST(:oid AS uuid) AND source = 'manual'"
        ),
        {"oid": oracle_id},
    )
    if quantity > 0:
        await conn.execute(
            text(
                "INSERT INTO wishlist (oracle_id, quantity, card_name, source) "
                "VALUES (CAST(:oid AS uuid), :quantity, :card_name, 'manual')"
            ),
            {
                "oid": oracle_id,
                "quantity": quantity,
                "card_name": name_row["name"],
            },
        )

    return {
        "object": "wishlist_card",
        "oracle_id": oracle_id,
        "card_name": name_row["name"],
        "quantity": quantity,
        "source": "manual",
    }


async def clear_wishlist(conn: _Connection) -> None:
    """Drop every wishlist row."""
    await conn.execute(text("DELETE FROM wishlist"))
