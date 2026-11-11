"""Commander discovery — D17.2 `GET /commanders/suggest`.

Closes the "I want an Aristocrats commander in Mardu" gap for MCP
agents. Filter `cards.is_commander = true`; optional `colors` (WUBRG
string) enforces `color_identity <@ :ci`; optional `tags` (comma-list
of Scryfall Tagger slugs) ANDs one `EXISTS` per slug against the
`oracle_tags` side table. Order by `edhrec_rank ASC NULLS LAST`,
secondary `id ASC`.

Response: Scryfall `List` envelope (no `total_cards` — the caller
usually wants the top-N and paginates via `limit`, not `page`).

The DSL near-equivalent is
`/cards/search?q=is:commander id<=WUB otag:aristocrats&order=edhrec` —
this endpoint bakes the same recipe behind a MCP-first URL that agents
find without guessing the DSL sugar.
"""

from __future__ import annotations

from typing import Any, Protocol

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import text

from biblioplex.errors import ErrorEnvelope
from biblioplex.errors import scryfall_error as _scryfall_error
from biblioplex.projection import ProjectionError, parse_projection, project

_ERROR_RESPONSE: dict[str, Any] = {
    "model": ErrorEnvelope,
    "description": "Scryfall-shaped error envelope.",
}


class _Engine(Protocol):
    def connect(self) -> object: ...


def get_read_engine(request: Request) -> _Engine:
    engine: _Engine = request.app.state.read_engine
    return engine


router = APIRouter(prefix="/commanders", tags=["commanders"])


_WUBRG_SET = frozenset("WUBRG")


def _parse_ci(raw: str) -> list[str] | None:
    """Normalize a WUBRG string to canonical upper-case letters.

    Any non-WUBRG char → `None` (422 at the router). Case-insensitive
    on input. Empty input is a legitimate no-op signal for the caller
    (translated to "no CI filter" upstream), not an error.
    """
    letters: list[str] = []
    for ch in raw.upper():
        if ch not in _WUBRG_SET:
            return None
        if ch not in letters:
            letters.append(ch)
    return letters


@router.get(
    "/suggest",
    summary="Suggest commanders by oracle tags + color identity",
    description=(
        "Filter `is_commander = true`; optional `colors` (WUBRG string) "
        "narrows to commanders whose color identity fits within the "
        "selected colors; optional `tags` (comma-list of Scryfall Tagger "
        "slugs) requires every slug to be present on the card. Sorted "
        "by `edhrec_rank ASC NULLS LAST`. Unknown tag slugs silently "
        "return zero rows — agents commonly guess slugs and 422 would "
        "be user-hostile."
    ),
    responses={422: _ERROR_RESPONSE},
)
async def suggest_commanders(
    tags: str | None = Query(
        default=None,
        description=(
            "Comma-separated Scryfall Tagger slugs (case-insensitive; "
            "AND-joined). Example: `sacrifice-outlet,graveyard-recursion`."
        ),
        examples=["ramp", "sacrifice-outlet,graveyard-recursion"],
    ),
    colors: str | None = Query(
        default=None,
        description=(
            "WUBRG string; matches commanders whose `color_identity` fits "
            "within these colors (subset). Case-insensitive."
        ),
        examples=["WUB", "RG"],
    ),
    limit: int = Query(
        default=25,
        ge=1,
        le=175,
        description="Max commanders to return (1–175, default 25).",
    ),
    fields: str | None = Query(
        default=None,
        description=(
            "Comma-separated dotted-path projection per §7. Shorter alias "
            "for `?projection=` — both names accept identical CSV syntax; "
            "when both are supplied, `fields` wins (D15.29)."
        ),
        examples=["name,color_identity"],
    ),
    projection: str | None = Query(
        default=None,
        include_in_schema=False,
    ),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Compile the filter set to one SELECT and return a Scryfall `List`."""
    # D15.29 — `?fields=` alias wins when both supplied.
    projection = fields if fields is not None else projection
    where_parts: list[str] = ["c.is_commander = true"]
    params: dict[str, Any] = {"limit": limit}

    if colors is not None and colors.strip():
        parsed = _parse_ci(colors.strip())
        if parsed is None:
            return _scryfall_error(
                422,
                "bad_request",
                f"invalid colors '{colors}' (use WUBRG letters)",
            )
        where_parts.append("c.color_identity <@ CAST(:ci AS text[])")
        params["ci"] = parsed

    if tags is not None and tags.strip():
        slugs = [s.strip().lower() for s in tags.split(",") if s.strip()]
        for i, slug in enumerate(slugs):
            key = f"tag_{i}"
            where_parts.append(
                f"EXISTS (SELECT 1 FROM oracle_tags ot "  # noqa: S608
                f"WHERE ot.oracle_id = c.oracle_id AND ot.tag = :{key})"
            )
            params[key] = slug

    where_sql = " AND ".join(where_parts)
    stmt = text(
        "SELECT c.raw, c.predicted_labels, c.label_scores "  # noqa: S608
        "FROM cards c "
        f"WHERE {where_sql} "
        "ORDER BY c.edhrec_rank ASC NULLS LAST, c.id ASC "
        "LIMIT :limit"
    )

    async with engine.connect() as conn:  # type: ignore[attr-defined]
        result = await conn.execute(stmt, params)
        rows = result.mappings().all()

    projection_paths: list[list[str | int]] | None
    if projection is not None and projection.strip():
        try:
            projection_paths = parse_projection(projection)
        except ProjectionError as exc:
            return _scryfall_error(422, "invalid_projection", str(exc))
    else:
        projection_paths = None

    data: list[dict[str, Any]] = []
    for row in rows:
        raw = dict(row["raw"]) if row["raw"] else {}
        raw["predicted_labels"] = list(row["predicted_labels"] or [])
        raw["label_scores"] = dict(row["label_scores"] or {})
        entry = project(raw, projection_paths) if projection_paths else raw
        data.append(entry)

    return {
        "object": "list",
        "data": data,
        "has_more": len(data) == limit,
    }
