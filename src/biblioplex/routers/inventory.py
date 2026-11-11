"""Inventory API router per D18.3."""

from __future__ import annotations

from typing import Any, Literal, Protocol

import httpx
from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import text

from biblioplex.errors import scryfall_error as _scryfall_error
from biblioplex.inventory.deckbox import (
    DeckboxFetchError,
    fetch_deckbox_csv,
    is_fatal_warning,
    parse_deckbox_csv,
)
from biblioplex.inventory.service import (
    clear_inventory,
    get_inventory_summary,
    list_inventory,
    resolve_and_sync_inventory,
    update_card_quantity,
)
from biblioplex.inventory.uploads import record_upload
from biblioplex.routers.cards import get_read_engine


class _Engine(Protocol):
    """Read-pool engine. ``begin()`` is the write path — SQLAlchemy's
    ``connect()`` opens an implicit transaction that is ROLLED BACK on
    close, so every INSERT/DELETE here must go through ``begin()``."""

    def connect(self) -> Any: ...

    def begin(self) -> Any: ...


router = APIRouter(prefix="/inventory", tags=["inventory"])


class _UnusableCsvError(ValueError):
    """The payload was not a usable Deckbox CSV export."""


async def _sync_csv_text(
    conn: Any,
    csv_text: str,
    mode: str,
    deckbox_url: str | None = None,
    filename: str | None = None,
) -> dict[str, Any]:
    """Parse a Deckbox CSV and apply it. Shared by every sync entrypoint.

    Raises ``_UnusableCsvError`` BEFORE issuing any statement when the text
    is not a usable export — a `mode: replace` on an unparseable payload
    would delete the whole inventory and report success.
    """
    items, warnings = parse_deckbox_csv(csv_text)
    fatal = [w for w in warnings if is_fatal_warning(w)]
    if fatal:
        raise _UnusableCsvError(f"Not a usable Deckbox CSV export: {fatal[0]}")
    result: dict[str, Any] = await resolve_and_sync_inventory(
        conn,
        items,
        mode=mode,
        source="deckbox",
        deckbox_url=deckbox_url,
    )
    result["warnings"] = warnings
    # Same transaction as the sync, so history never shows a rolled-back
    # import. Recorded only on success — a 422 never reaches here.
    await record_upload(
        conn,
        kind="inventory",
        filename=filename or "pasted.csv",
        content=csv_text.encode("utf-8"),
        mode=mode,
        result=result,
    )
    return result


class SyncDeckboxRequest(BaseModel):
    """Payload for Deckbox synchronization."""

    url: str | None = Field(
        default=None,
        description="Deckbox set URL (e.g. https://deckbox.org/sets/3293466) or bare set ID",
        examples=["https://deckbox.org/sets/3293466", "3293466"],
    )
    set_id: str | None = Field(
        default=None,
        description="Deckbox set numerical ID",
        examples=["3293466"],
    )
    csv_text: str | None = Field(
        default=None,
        description="Raw Deckbox CSV export text content",
        examples=["Count,Name,Edition\n1,Sol Ring,Commander 2021\n"],
    )
    mode: Literal["replace", "merge"] = Field(
        default="replace",
        description="Sync mode: 'replace' wipes existing snapshot, 'merge' upserts counts",
        examples=["replace"],
    )


@router.post(
    "/sync/deckbox",
    summary="Sync collection inventory from Deckbox URL or CSV text",
    description=(
        "Synchronizes user collection inventory from Deckbox. Accepts a Deckbox set URL "
        "(e.g. `https://deckbox.org/sets/3293466`), bare set ID, or direct CSV text. "
        "When called without parameters, re-syncs using the previously saved Deckbox URL. "
        "Auto-registered as MCP tool `sync_deckbox_inventory`."
    ),
)
async def sync_deckbox_inventory(
    req: SyncDeckboxRequest | None = None,
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Sync collection inventory from Deckbox URL, set ID, or CSV text."""
    body = req or SyncDeckboxRequest()
    deckbox_target_url: str | None = None
    csv_text: str | None = body.csv_text

    async with engine.begin() as conn:
        try:
            if body.url or body.set_id:
                target = body.url or body.set_id
                deckbox_target_url, csv_text = await fetch_deckbox_csv(target)  # type: ignore[arg-type]
            elif not csv_text:
                # Check for saved URL in inventory_sync_settings
                res = await conn.execute(
                    text("SELECT deckbox_url FROM inventory_sync_settings WHERE id = 1")
                )
                s_row = res.mappings().one_or_none()
                if s_row and s_row["deckbox_url"]:
                    deckbox_target_url, csv_text = await fetch_deckbox_csv(
                        s_row["deckbox_url"]
                    )
                else:
                    return _scryfall_error(
                        422,
                        "bad_request",
                        "A Deckbox URL, set ID, or csv_text is required for sync",
                    )
        except DeckboxFetchError as exc:
            return _scryfall_error(422, "bad_request", str(exc))
        except httpx.HTTPError as exc:
            return _scryfall_error(
                502, "bad_gateway", f"Could not reach Deckbox: {exc}"
            )

        try:
            return await _sync_csv_text(
                conn, csv_text, body.mode, deckbox_target_url
            )
        except _UnusableCsvError as exc:
            return _scryfall_error(422, "bad_request", str(exc))


@router.post(
    "/sync/upload",
    summary="Sync collection inventory from an uploaded Deckbox CSV file",
    description=(
        "Multipart upload of a Deckbox CSV export (`file=@collection.csv`). "
        "Same parsing, resolution, and `replace`/`merge` semantics as "
        "`/inventory/sync/deckbox` — this is the form to use for automation, "
        "since Deckbox requires a logged-in session to export a set by URL "
        "and the URL path therefore cannot run unattended. "
        "Auto-registered as MCP tool `upload_deckbox_inventory`."
    ),
)
async def upload_deckbox_inventory(
    file: UploadFile = File(description="Deckbox CSV export"),
    mode: Literal["replace", "merge"] = Form(default="replace"),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Sync collection inventory from an uploaded Deckbox CSV file."""
    raw = await file.read()
    try:
        # Deckbox exports UTF-8; `errors="replace"` keeps a stray byte from
        # 500-ing a 3000-line collection over one card name.
        csv_text = raw.decode("utf-8", errors="replace")
    except (UnicodeDecodeError, AttributeError):
        return _scryfall_error(422, "bad_request", "Uploaded file is not text")

    async with engine.begin() as conn:
        try:
            return await _sync_csv_text(
                conn, csv_text, mode, filename=file.filename
            )
        except _UnusableCsvError as exc:
            return _scryfall_error(422, "bad_request", str(exc))


@router.get(
    "",
    summary="List owned inventory cards with pagination and search",
    description=(
        "Returns a paginated list of cards in the user's inventory. "
        "Supports optional case-insensitive substring search `?q=`. "
        "Auto-registered as MCP tool `list_inventory_cards`."
    ),
)
async def list_inventory_cards(
    q: str | None = Query(default=None, description="Optional card name search filter"),
    page: int = Query(default=1, ge=1, description="Page number (1-indexed)"),
    limit: int = Query(default=50, ge=1, le=200, description="Items per page"),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """List owned inventory cards."""
    async with engine.connect() as conn:
        return await list_inventory(conn, q=q, page=page, limit=limit)


@router.get(
    "/summary",
    summary="Get summary metrics of current inventory",
    description=(
        "Returns overview statistics of user collection inventory including total card count, "
        "distinct cards count, estimated total value in USD, and last sync timestamp. "
        "Auto-registered as MCP tool `get_inventory_summary_stats`."
    ),
)
async def get_inventory_summary_stats(
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Get overview statistics of current inventory."""
    async with engine.connect() as conn:
        return await get_inventory_summary(conn)


@router.get(
    "/oracle-ids",
    summary="Flat list of every owned oracle id",
    description=(
        "Returns just the distinct `oracle_id` values with a positive owned "
        "quantity — no card payloads, no pagination. Exists so a client can "
        "build an ownership lookup in one request and badge a whole page of "
        "search results locally instead of asking per card. "
        "Auto-registered as MCP tool `list_owned_oracle_ids`."
    ),
)
async def list_owned_oracle_ids(
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """List the distinct oracle ids present in the inventory."""
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT DISTINCT oracle_id FROM inventory WHERE quantity > 0"
                )
            )
        ).all()
    return {"object": "list", "data": [str(r.oracle_id) for r in rows]}


@router.delete(
    "",
    summary="Clear entire inventory collection",
    description=(
        "Deletes all stored inventory items and resets sync metadata. "
        "Auto-registered as MCP tool `clear_inventory_collection`."
    ),
)
async def clear_inventory_collection(
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Clear all inventory items."""
    async with engine.begin() as conn:
        await clear_inventory(conn)
        return {"object": "inventory_cleared", "status": "ok"}


class UpsertCardRequest(BaseModel):
    """Payload for a manual per-card quantity tweak."""

    oracle_id: str = Field(
        description="Oracle id of the card whose owned quantity is being set",
        examples=["4bf2b7f4-4b3c-4b8e-9a0e-000000000000"],
    )
    quantity: int = Field(
        default=1,
        ge=0,
        description="Owned non-foil quantity. 0 removes the card from inventory.",
        examples=[2],
    )
    foil_quantity: int = Field(
        default=0,
        ge=0,
        description="Owned foil quantity",
        examples=[1],
    )


@router.post(
    "/cards",
    summary="Set the owned quantity of a single card",
    description=(
        "Manual override for one card's owned quantity, bypassing a full Deckbox "
        "sync. `quantity: 0` deletes the row. Rows written here carry "
        "`source = 'manual'` so a subsequent `mode: replace` Deckbox sync (which "
        "only clears `source = 'deckbox'`) leaves them intact. "
        "Auto-registered as MCP tool `upsert_inventory_card`."
    ),
)
async def upsert_inventory_card(
    req: UpsertCardRequest,
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Set the owned quantity of a single card by oracle id."""
    async with engine.begin() as conn:
        updated = await update_card_quantity(
            conn,
            oracle_id=req.oracle_id,
            quantity=req.quantity,
            foil_quantity=req.foil_quantity,
        )
    if updated is None:
        return _scryfall_error(
            404, "not_found", f"No card with oracle_id {req.oracle_id}"
        )
    return updated
