"""Import-history routes per D18.17."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from biblioplex.errors import scryfall_error as _scryfall_error
from biblioplex.inventory.uploads import delete_upload, get_upload, list_uploads
from biblioplex.routers.cards import get_read_engine
from biblioplex.routers.inventory import _Engine

router = APIRouter(prefix="/uploads", tags=["uploads"])


@router.get(
    "",
    summary="List previously imported CSV files",
    description=(
        "Newest-first history of every CSV applied through a sync endpoint, "
        "with the row counts each import produced. File content is excluded "
        "here — fetch it from `/uploads/{id}/download`. "
        "Auto-registered as MCP tool `list_uploads_history`."
    ),
)
async def list_uploads_history(
    kind: Literal["inventory", "wishlist"] | None = Query(
        default=None, description="Filter to one collection"
    ),
    limit: int = Query(default=50, ge=1, le=200, description="Max rows"),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """List previously imported CSV files."""
    async with engine.connect() as conn:
        return await list_uploads(conn, kind=kind, limit=limit)


@router.get(
    "/{upload_id}/download",
    summary="Download a previously imported CSV verbatim",
    description=(
        "Streams back the exact bytes that were imported, so a past sync can "
        "be re-applied or diffed against a newer export."
    ),
    response_class=Response,
)
async def download_upload(
    upload_id: str,
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Download one stored import."""
    async with engine.connect() as conn:
        row = await get_upload(conn, upload_id)
    if row is None:
        return _scryfall_error(404, "not_found", f"No upload with id {upload_id}")
    # Quote the filename: Deckbox names them `Inventory_user_2026.August.17.csv`
    # — dots are fine unquoted but a future name with a space would not be.
    return Response(
        content=row["content"],
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{row["filename"]}"'
        },
    )


@router.delete(
    "/{upload_id}",
    summary="Delete one stored import",
    description=(
        "Removes a file from the import history. Does not touch the "
        "inventory or wishlist rows it produced."
    ),
)
async def delete_upload_row(
    upload_id: str,
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Delete one stored import."""
    async with engine.begin() as conn:
        deleted = await delete_upload(conn, upload_id)
    if not deleted:
        return _scryfall_error(404, "not_found", f"No upload with id {upload_id}")
    return {"object": "upload_deleted", "id": upload_id}
