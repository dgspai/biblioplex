"""Import history per D18.17.

Every CSV applied through a sync endpoint is stored verbatim so an operator
can re-download exactly what was imported, and diff one sync against another.

Content is a ``bytea`` column rather than a file on disk: a Deckbox export is
under a megabyte, they arrive a few times a month, and a column needs no
volume mount, no path configuration and no separate backup story. The
listing endpoint never selects ``content`` — only the download route does —
so browsing history costs nothing extra.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from biblioplex.inventory.service import _Connection

# Belt-and-braces ceiling. FastAPI has already buffered the body by the time
# we see it, so this bounds what reaches the DB, not what reaches the process.
MAX_UPLOAD_BYTES = 32 * 1024 * 1024


async def record_upload(
    conn: _Connection,
    kind: str,
    filename: str,
    content: bytes,
    mode: str,
    result: dict[str, Any],
) -> None:
    """Persist one applied import alongside its outcome counts.

    Called only after the sync succeeds, inside the same transaction, so the
    history never shows an import that was rolled back.
    """
    await conn.execute(
        text(
            "INSERT INTO uploads "
            "(kind, filename, content, size_bytes, mode, "
            " total_cards, distinct_cards, unmatched_count) "
            "VALUES (:kind, :filename, :content, :size_bytes, :mode, "
            " :total_cards, :distinct_cards, :unmatched_count)"
        ),
        {
            "kind": kind,
            "filename": filename,
            "content": content,
            "size_bytes": len(content),
            "mode": mode,
            "total_cards": int(result.get("total_cards") or 0),
            "distinct_cards": int(result.get("distinct_cards") or 0),
            "unmatched_count": len(result.get("unmatched") or []),
        },
    )


async def list_uploads(
    conn: _Connection,
    kind: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Newest-first import history. Never selects ``content``."""
    where = "WHERE kind = :kind " if kind else ""
    params: dict[str, Any] = {"limit": limit}
    if kind:
        params["kind"] = kind

    rows = (
        await conn.execute(
            text(
                "SELECT id, kind, filename, size_bytes, mode, total_cards, "  # noqa: S608
                "       distinct_cards, unmatched_count, uploaded_at "
                f"FROM uploads {where}"
                "ORDER BY uploaded_at DESC LIMIT :limit"
            ),
            params,
        )
    ).mappings().all()

    return {
        "object": "list",
        "data": [
            {
                "id": str(r["id"]),
                "kind": r["kind"],
                "filename": r["filename"],
                "size_bytes": r["size_bytes"],
                "mode": r["mode"],
                "total_cards": r["total_cards"],
                "distinct_cards": r["distinct_cards"],
                "unmatched_count": r["unmatched_count"],
                "uploaded_at": r["uploaded_at"].isoformat(),
            }
            for r in rows
        ],
    }


async def get_upload(conn: _Connection, upload_id: str) -> dict[str, Any] | None:
    """Fetch one stored import including its bytes, or None if unknown."""
    row = (
        await conn.execute(
            text(
                "SELECT id, kind, filename, content, uploaded_at "
                "FROM uploads WHERE id = CAST(:id AS uuid)"
            ),
            {"id": upload_id},
        )
    ).mappings().one_or_none()
    if row is None:
        return None
    return {
        "id": str(row["id"]),
        "kind": row["kind"],
        "filename": row["filename"],
        "content": bytes(row["content"]),
    }


async def delete_upload(conn: _Connection, upload_id: str) -> bool:
    """Delete one stored import. Returns False when the id was unknown."""
    result = await conn.execute(
        text("DELETE FROM uploads WHERE id = CAST(:id AS uuid)"),
        {"id": upload_id},
    )
    return bool(getattr(result, "rowcount", 0))
