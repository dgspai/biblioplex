"""Catalog discovery — D17.4 ``GET /tags``, ``GET /keywords``, ``GET /sets``.

The biggest MCP blocker before D17.4 was slug guessing: agents couldn't
tell ``otag:aristocrats`` from ``otag:aristocrat`` without picking a
random guess and hoping. These endpoints enumerate the corpus-canonical
labels with hit counts so agents can autocomplete + rank before
crafting a DSL query.

* ``/tags`` — oracle-tag slugs from the ``oracle_tags`` side table
  (Scryfall Tagger). Backs ``otag:``.
* ``/keywords`` — Scryfall keyword strings extracted from
  ``raw->'keywords'``. Backs the pending ``keyword:`` predicate
  (D15.30).
* ``/sets`` — set codes + names + release dates. Backs the pending
  ``set:`` predicate.

All three cache with a weak ETag keyed on the refresh epoch (D13.5
pattern) so a completed ``/admin/refresh`` invalidates every catalog
response uniformly.

Bug.27 discipline: ``CAST(:X AS TYPE)`` — never ``:X::TYPE`` — because
SQLAlchemy ``text()`` swallows the leading ``:X`` before the ``::``.
"""

from __future__ import annotations

import json
from hashlib import sha1
from typing import Any, Protocol

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import text

from biblioplex.errors import ErrorEnvelope

_ERROR_RESPONSE: dict[str, Any] = {
    "model": ErrorEnvelope,
    "description": "Scryfall-shaped error envelope.",
}


class _Engine(Protocol):
    def connect(self) -> object: ...


def get_read_engine(request: Request) -> _Engine:
    engine: _Engine = request.app.state.read_engine
    return engine


router = APIRouter(tags=["catalog"])


# Catalog responses are stable per refresh epoch. Short TTL keeps busy
# clients on the edge cache; SWR gives them a grace window before they
# have to re-hit the DB. Mirrors the /cards/search cache posture from
# D13.5 — same reasoning, same knobs.
_CATALOG_CACHE_HEADERS_TEMPLATE = (
    "public, max-age=300, stale-while-revalidate=600"
)


def _catalog_etag(*axes: str | int) -> str:
    """Weak ETag over ``(endpoint, params, refresh-epoch)``.

    Weak because the payload can differ under the same tuple only via
    a race-window edge case (a concurrent refresh finishing between the
    request start and the SELECT); the wire semantics are stable.
    """
    key = "|".join(str(a) for a in axes)
    return f'W/"{sha1(key.encode(), usedforsecurity=False).hexdigest()}"'


def _cache_headers(etag: str) -> dict[str, str]:
    return {
        "Cache-Control": _CATALOG_CACHE_HEADERS_TEMPLATE,
        "ETag": etag,
    }


@router.get(
    "/tags",
    summary="List oracle-tag slugs with hit counts",
    description=(
        "Enumerate every Scryfall Tagger slug present in the "
        "``oracle_tags`` side table, ranked by cards-carrying-tag "
        "count (DESC). Backs the ``otag:`` DSL predicate — agents "
        "use this to autocomplete slugs before crafting queries "
        "instead of guessing ``otag:aristocrats`` vs ``otag:aristocrat``."
    ),
    responses={422: _ERROR_RESPONSE},
)
async def list_tags(
    request: Request,
    prefix: str | None = Query(
        default=None,
        description=(
            "Optional case-insensitive prefix filter. Compiled to "
            "``tag ILIKE :prefix || '%'``. Empty / unset = unfiltered."
        ),
        examples=["arist", "ramp", "sacrifice"],
    ),
    limit: int = Query(
        default=50,
        ge=1,
        le=500,
        description="Max rows (1-500, default 50).",
    ),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Compile the one aggregate SELECT and return a Scryfall ``List``."""
    epoch = request.app.state.refresh_status.epoch()
    etag = _catalog_etag("tags", prefix or "", limit, epoch)
    if request.headers.get("If-None-Match") == etag:
        return Response(status_code=304, headers=_cache_headers(etag))

    where_sql = ""
    params: dict[str, Any] = {"limit": limit}
    if prefix is not None and prefix.strip():
        where_sql = "WHERE tag ILIKE :prefix || '%' "
        params["prefix"] = prefix

    stmt = text(
        "SELECT tag AS slug, count(*) AS count "  # noqa: S608
        "FROM oracle_tags "
        f"{where_sql}"
        "GROUP BY tag "
        "ORDER BY count(*) DESC, tag ASC "
        "LIMIT :limit"
    )

    async with engine.connect() as conn:  # type: ignore[attr-defined]
        result = await conn.execute(stmt, params)
        rows = result.mappings().all()

    data = [{"slug": row["slug"], "count": int(row["count"])} for row in rows]
    body = {
        "object": "list",
        "data": data,
        "has_more": len(data) == limit,
    }
    return Response(
        content=_json_bytes(body),
        media_type="application/json",
        headers=_cache_headers(etag),
    )


@router.get(
    "/keywords",
    summary="List Scryfall keyword strings with hit counts",
    description=(
        "Enumerate every keyword string in ``raw->'keywords'`` across "
        "the corpus, ranked by cards-carrying-keyword count (DESC). "
        "Backs the pending ``keyword:`` DSL predicate (D15.30). Casing "
        "matches Scryfall's canonical form (``Flying``, ``Ward``)."
    ),
    responses={422: _ERROR_RESPONSE},
)
async def list_keywords(
    request: Request,
    prefix: str | None = Query(
        default=None,
        description=(
            "Optional case-insensitive prefix filter. Compiled to "
            "``k ILIKE :prefix || '%'``. Empty / unset = unfiltered."
        ),
        examples=["fly", "war", "menace"],
    ),
    limit: int = Query(
        default=50,
        ge=1,
        le=500,
        description="Max rows (1-500, default 50).",
    ),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Fan out ``raw->'keywords'`` and aggregate hit counts per keyword."""
    epoch = request.app.state.refresh_status.epoch()
    etag = _catalog_etag("keywords", prefix or "", limit, epoch)
    if request.headers.get("If-None-Match") == etag:
        return Response(status_code=304, headers=_cache_headers(etag))

    where_sql = ""
    params: dict[str, Any] = {"limit": limit}
    if prefix is not None and prefix.strip():
        where_sql = "WHERE k ILIKE :prefix || '%' "
        params["prefix"] = prefix

    stmt = text(
        "SELECT k AS keyword, count(*) AS count "  # noqa: S608
        "FROM cards, jsonb_array_elements_text(raw->'keywords') AS k "
        f"{where_sql}"
        "GROUP BY k "
        "ORDER BY count(*) DESC, k ASC "
        "LIMIT :limit"
    )

    async with engine.connect() as conn:  # type: ignore[attr-defined]
        result = await conn.execute(stmt, params)
        rows = result.mappings().all()

    data = [{"keyword": row["keyword"], "count": int(row["count"])} for row in rows]
    body = {
        "object": "list",
        "data": data,
        "has_more": len(data) == limit,
    }
    return Response(
        content=_json_bytes(body),
        media_type="application/json",
        headers=_cache_headers(etag),
    )


@router.get(
    "/sets",
    summary="List Scryfall set codes with release dates and hit counts",
    description=(
        "Enumerate every set present in the corpus with "
        "``{code, name, released_at, count}``. Ordered by release date "
        "DESC so the most recent set surfaces first. Bounded universe "
        "(<1k sets) — no prefix / limit knobs, agents get the whole "
        "catalog in one payload."
    ),
    responses={422: _ERROR_RESPONSE},
)
async def list_sets(
    request: Request,
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """One aggregate over cards + release date. Cached per epoch."""
    epoch = request.app.state.refresh_status.epoch()
    etag = _catalog_etag("sets", epoch)
    if request.headers.get("If-None-Match") == etag:
        return Response(status_code=304, headers=_cache_headers(etag))

    stmt = text(
        "SELECT raw->>'set' AS code, "
        "       raw->>'set_name' AS name, "
        "       MIN(CAST(raw->>'released_at' AS date)) AS released_at, "
        "       count(*) AS count "
        "FROM cards "
        "GROUP BY raw->>'set', raw->>'set_name' "
        "ORDER BY MIN(CAST(raw->>'released_at' AS date)) DESC NULLS LAST"
    )

    async with engine.connect() as conn:  # type: ignore[attr-defined]
        result = await conn.execute(stmt)
        rows = result.mappings().all()

    data: list[dict[str, Any]] = []
    for row in rows:
        released = row["released_at"]
        # `date` objects need isoformat() to json-serialize. String
        # inputs already survive; other/None fall through as-is.
        released_str: str | None
        if released is None:
            released_str = None
        elif hasattr(released, "isoformat"):
            released_str = released.isoformat()
        else:
            released_str = str(released)
        data.append(
            {
                "code": row["code"],
                "name": row["name"],
                "released_at": released_str,
                "count": int(row["count"]),
            }
        )
    body = {
        "object": "list",
        "data": data,
    }
    return Response(
        content=_json_bytes(body),
        media_type="application/json",
        headers=_cache_headers(etag),
    )


def _json_bytes(obj: Any) -> bytes:
    """Serialize with the stdlib — no Pydantic round-trip.

    Same pattern as `errors.scryfall_error`: the payload shape is a
    handful of primitives; we skip the pydantic model round-trip on
    the hot path.
    """
    return json.dumps(obj, separators=(",", ":")).encode()
