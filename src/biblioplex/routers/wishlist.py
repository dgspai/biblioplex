"""Wishlist API router per D18.8.

Mirrors `/inventory/*`. There is no URL-sync route here: Deckbox requires a
logged-in session to export a set (todo.md Bug.32), so CSV upload / paste is
the only path that actually runs unattended.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import text

from biblioplex.errors import scryfall_error as _scryfall_error
from biblioplex.inventory.deckbox import is_fatal_warning, parse_deckbox_csv
from biblioplex.inventory.uploads import record_upload
from biblioplex.inventory.wishlist import (
    clear_wishlist,
    get_wishlist_summary,
    list_wishlist,
    sync_wishlist,
    update_wishlist_quantity,
)
from biblioplex.routers.cards import get_read_engine
from biblioplex.routers.inventory import _Engine, _UnusableCsvError

router = APIRouter(prefix="/wishlist", tags=["wishlist"])


async def _sync_csv_text(
    conn: Any, csv_text: str, mode: str, filename: str | None = None
) -> dict[str, Any]:
    """Parse a Deckbox CSV and apply it to the wishlist.

    Raises ``_UnusableCsvError`` BEFORE issuing any statement when the text
    is not a usable export — `mode: replace` on an unparseable payload would
    delete the whole wishlist and report success (the D18.3 Bug.32 shape).
    """
    items, warnings = parse_deckbox_csv(csv_text)
    fatal = [w for w in warnings if is_fatal_warning(w)]
    if fatal:
        raise _UnusableCsvError(f"Not a usable Deckbox CSV export: {fatal[0]}")
    result: dict[str, Any] = await sync_wishlist(conn, items, mode=mode)
    result["warnings"] = warnings
    await record_upload(
        conn,
        kind="wishlist",
        filename=filename or "pasted.csv",
        content=csv_text.encode("utf-8"),
        mode=mode,
        result=result,
    )
    return result


class SyncWishlistRequest(BaseModel):
    """Raw-CSV payload for a wishlist sync."""

    csv_text: str = Field(
        description="Raw Deckbox wishlist CSV export text",
        examples=["Count,Name,Edition\n1,Mana Crypt,Eternal Masters\n"],
    )
    mode: Literal["replace", "merge"] = Field(
        default="replace",
        description="'replace' swaps the whole snapshot, 'merge' only the listed cards",
        examples=["replace"],
    )


@router.post(
    "/sync",
    summary="Sync the wishlist from Deckbox CSV text",
    description=(
        "Applies a Deckbox wishlist CSV export supplied as raw text. Same "
        "parser, name resolution and `replace`/`merge` semantics as the "
        "inventory sync. Auto-registered as MCP tool `sync_wishlist_csv`."
    ),
)
async def sync_wishlist_csv(
    req: SyncWishlistRequest,
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Sync the wishlist from raw Deckbox CSV text."""
    async with engine.begin() as conn:
        try:
            return await _sync_csv_text(conn, req.csv_text, req.mode)
        except _UnusableCsvError as exc:
            return _scryfall_error(422, "bad_request", str(exc))


@router.post(
    "/sync/upload",
    summary="Sync the wishlist from an uploaded Deckbox CSV file",
    description=(
        "Multipart upload of a Deckbox wishlist CSV export "
        "(`file=@wishlist.csv`). The automation path — Deckbox requires a "
        "logged-in session to export by URL. "
        "Auto-registered as MCP tool `upload_wishlist_csv`."
    ),
)
async def upload_wishlist_csv(
    file: UploadFile = File(description="Deckbox wishlist CSV export"),
    mode: Literal["replace", "merge"] = Form(default="replace"),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Sync the wishlist from an uploaded Deckbox CSV file."""
    raw = await file.read()
    csv_text = raw.decode("utf-8", errors="replace")
    async with engine.begin() as conn:
        try:
            return await _sync_csv_text(
                conn, csv_text, mode, filename=file.filename
            )
        except _UnusableCsvError as exc:
            return _scryfall_error(422, "bad_request", str(exc))


@router.get(
    "",
    summary="List wanted cards with pagination and search",
    description=(
        "Paginated wishlist listing. Each row carries `owned_quantity` so a "
        "client can flag entries the user has since acquired. "
        "Auto-registered as MCP tool `list_wishlist_cards`."
    ),
)
async def list_wishlist_cards(
    q: str | None = Query(default=None, description="Optional card name search filter"),
    page: int = Query(default=1, ge=1, description="Page number (1-indexed)"),
    limit: int = Query(default=50, ge=1, le=200, description="Items per page"),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """List wanted cards."""
    async with engine.connect() as conn:
        return await list_wishlist(conn, q=q, page=page, limit=limit)


@router.get(
    "/summary",
    summary="Get summary metrics of the wishlist",
    description=(
        "Total wanted cards, distinct cards, estimated USD value, how many "
        "wanted cards are already owned, and the last sync timestamp "
        "(derived from the rows themselves). "
        "Auto-registered as MCP tool `get_wishlist_summary_stats`."
    ),
)
async def get_wishlist_summary_stats(
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Get overview statistics of the wishlist."""
    async with engine.connect() as conn:
        return await get_wishlist_summary(conn)


@router.get(
    "/oracle-ids",
    summary="Flat list of every wanted oracle id",
    description=(
        "Distinct `oracle_id` values with a positive wanted quantity — no "
        "card payloads, no pagination. Lets a client badge a whole page of "
        "search results in one request. "
        "Auto-registered as MCP tool `list_wanted_oracle_ids`."
    ),
)
async def list_wanted_oracle_ids(
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """List the distinct oracle ids present in the wishlist."""
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT DISTINCT oracle_id FROM wishlist WHERE quantity > 0")
            )
        ).all()
    return {"object": "list", "data": [str(r.oracle_id) for r in rows]}


class UpsertWishlistCardRequest(BaseModel):
    """Payload for a manual per-card wishlist tweak."""

    oracle_id: str = Field(
        description="Oracle id of the card whose wanted quantity is being set",
        examples=["4bf2b7f4-4b3c-4b8e-9a0e-000000000000"],
    )
    quantity: int = Field(
        default=1,
        ge=0,
        description="Wanted quantity. 0 removes the card from the wishlist.",
        examples=[1],
    )


@router.post(
    "/cards",
    summary="Set the wanted quantity of a single card",
    description=(
        "Manual override for one card, bypassing a full sync. `quantity: 0` "
        "deletes the row. Rows written here carry `source = 'manual'` so a "
        "`mode: replace` Deckbox sync (which only clears `source = "
        "'deckbox'`) leaves them intact. "
        "Auto-registered as MCP tool `upsert_wishlist_card`."
    ),
)
async def upsert_wishlist_card(
    req: UpsertWishlistCardRequest,
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Set the wanted quantity of a single card by oracle id."""
    async with engine.begin() as conn:
        updated = await update_wishlist_quantity(
            conn, oracle_id=req.oracle_id, quantity=req.quantity
        )
    if updated is None:
        return _scryfall_error(
            404, "not_found", f"No card with oracle_id {req.oracle_id}"
        )
    return updated


@router.delete(
    "",
    summary="Clear the entire wishlist",
    description=(
        "Deletes every wishlist row. "
        "Auto-registered as MCP tool `clear_wishlist_collection`."
    ),
)
async def clear_wishlist_collection(
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Clear all wishlist rows."""
    async with engine.begin() as conn:
        await clear_wishlist(conn)
    return {"object": "wishlist_cleared", "status": "ok"}
