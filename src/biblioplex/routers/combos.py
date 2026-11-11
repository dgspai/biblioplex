"""Commander Spellbook combo browse endpoints.

* ``GET /combos/search`` — filter/paginate the ``combos`` table by card
  membership, color-identity subset, produced result, and free-text
  prereqs/steps.
* ``GET /combos/{combo_id}`` — single combo with resolved card names.

Sibling of ``GET /cards/{id}/combos``: same SELECT shape (LATERAL card
name resolution) but sourced from the global ``combos`` table rather
than a per-card join. Response wire is Scryfall-shaped
``{"object": "list", "data": [...]}`` for consistency with the rest of
the API.
"""

from __future__ import annotations

from typing import Any, Protocol

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from biblioplex.errors import ErrorEnvelope
from biblioplex.errors import scryfall_error as _scryfall_error

_ERROR_RESPONSE: dict[str, Any] = {
    "model": ErrorEnvelope,
    "description": "Scryfall-shaped error envelope.",
}


class _Engine(Protocol):
    def connect(self) -> object: ...


def get_read_engine(request: Request) -> _Engine:
    engine: _Engine = request.app.state.read_engine
    return engine


router = APIRouter(prefix="/combos", tags=["combos"])


# ponytail: single SELECT reused across search + detail; card-name
# resolution stays in SQL (same LATERAL trick as /cards/:id/combos).
_COMBO_SELECT = (
    "SELECT co.id AS combo_id, "
    "       co.color_identity, "
    "       co.prereqs, "
    "       co.steps, "
    "       co.results, "
    "       ( "
    "         SELECT array_agg(cd.name ORDER BY t.ord) "
    "         FROM unnest(co.oracle_ids) WITH ORDINALITY t(oid, ord) "
    "         JOIN cards cd ON cd.oracle_id = t.oid "
    "       ) AS card_names, "
    "       ( "
    "         SELECT array_agg(cd.id::text ORDER BY t.ord) "
    "         FROM unnest(co.oracle_ids) WITH ORDINALITY t(oid, ord) "
    "         JOIN cards cd ON cd.oracle_id = t.oid "
    "       ) AS card_ids, "
    "       array_length(co.oracle_ids, 1) AS card_count "
    "FROM combos co "
)


_WUBRG_SET = frozenset("WUBRG")

# Cap pagination cost — combos table is small (~30K rows) so a large
# page is cheap, but a hostile client could still probe. 100 mirrors
# most public combo browsers.
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100


def _bad_request(details: str) -> JSONResponse:
    return _scryfall_error(400, "bad_request", details)


def _not_found(details: str) -> JSONResponse:
    return _scryfall_error(404, "not_found", details)


def _row_to_combo(r: Any) -> dict[str, Any]:
    return {
        "id": r.combo_id,
        "cards": list(r.card_names or []),
        "card_ids": list(r.card_ids or []),
        "color_identity": list(r.color_identity or []),
        "prereqs": r.prereqs,
        "steps": r.steps,
        "results": list(r.results or []),
        "card_count": r.card_count or 0,
    }


def _parse_ci(ci: str) -> list[str] | None:
    """Parse a `WUBRG`/`C` filter. Returns None when the string is invalid.

    `C` (colorless) parses to no letters, so `?ci=C` keeps the `<@` filter
    with an empty array and only colorless combos fit. Next to colors it is
    redundant: colorless combos already fit within any color set.

    Any other character (letter, digit, or punctuation) is invalid so the
    caller can 400. Silent-drop of digits/punct hid typos like `?ci=W1U`
    → treated as `WU`; D10.11 tightens the contract.
    """
    seen: list[str] = []
    for ch in ci.upper():
        if ch in _WUBRG_SET:
            if ch not in seen:
                seen.append(ch)
        elif ch != "C":
            return None
    return seen


@router.get(
    "/search",
    summary="Search Commander Spellbook combos",
    description=(
        "Paginated combo browse. Filters (all optional, ANDed):\n\n"
        "* `card` — repeatable card name (case-insensitive). Combo must "
        "contain every listed card.\n"
        "* `ci` — WUBRG string; combos whose color identity fits within "
        "the selected colors (Spellbook default). Empty = 5-color allowed; "
        "`C` = colorless only.\n"
        "* `result` — substring match against any produced result.\n"
        "* `q` — substring match against prereqs/steps prose.\n"
        "* `page`, `page_size` — 1-indexed pagination (page_size capped "
        f"at {MAX_PAGE_SIZE}).\n"
        "* `sort` — `id` (stable) or `cards` (card count asc).\n"
    ),
    responses={400: _ERROR_RESPONSE},
)
async def search_combos(
    card: list[str] = Query(default_factory=list),
    ci: str | None = Query(default=None),
    result: str | None = Query(default=None),
    q: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    sort: str = Query(default="id"),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    if sort not in ("id", "cards"):
        return _bad_request(f"unknown sort '{sort}' (want 'id' or 'cards')")

    where: list[str] = []
    params: dict[str, Any] = {}

    if ci is not None and ci:
        parsed = _parse_ci(ci)
        if parsed is None:
            return _bad_request(f"invalid color identity '{ci}' (use WUBRG or C)")
        # Subset match: combo CI is contained in selected colors.
        where.append("co.color_identity <@ CAST(:ci AS text[])")
        params["ci"] = parsed

    if result:
        where.append("EXISTS (SELECT 1 FROM unnest(co.results) r WHERE r ILIKE :result)")
        params["result"] = f"%{result}%"

    if q:
        where.append(
            "(COALESCE(co.prereqs, '') ILIKE :q OR COALESCE(co.steps, '') ILIKE :q)"
        )
        params["q"] = f"%{q}%"

    async with engine.connect() as conn:  # type: ignore[attr-defined]
        # Bug.13: resolve `card=` names → oracle_ids once, then use
        # `= ANY(co.oracle_ids)` per resolved id so `combos_oracle_ids_gin`
        # can back the WHERE. Prior correlated `EXISTS (SELECT 1 FROM
        # cards cd WHERE lower(cd.name) = lower(:card_i) AND ...)` forced
        # the planner into a seq-scan of `combos`.
        if card:
            lowered = [n.lower() for n in card]
            resolve_rows = (
                await conn.execute(
                    text(
                        "SELECT oracle_id, LOWER(name) AS lname FROM cards "
                        "WHERE LOWER(name) = ANY(:lowered)"
                    ),
                    {"lowered": lowered},
                )
            ).all()
            resolved = {r.lname: str(r.oracle_id) for r in resolve_rows}
            # Bug.23: unresolved name → 404 (aligned with /cards/search §6.1).
            # A name that has no oracle_id can't be in any combo, so surface
            # the zero-result path as a Scryfall-shaped 404 instead of an
            # empty envelope with total=0.
            if len(resolved) < len(set(lowered)):
                return _not_found(
                    f"No combos found for {sorted(set(lowered))}"
                )
            for i, name in enumerate(card):
                key = f"oid_{i}"
                where.append(f"CAST(:{key} AS uuid) = ANY(co.oracle_ids)")
                params[key] = resolved[name.lower()]

        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        order_sql = (
            "ORDER BY card_count ASC, co.id" if sort == "cards" else "ORDER BY co.id"
        )
        offset = (page - 1) * page_size

        count_stmt = text(f"SELECT COUNT(*) FROM combos co {where_sql}")  # noqa: S608
        total = (await conn.execute(count_stmt, params)).scalar_one()
        list_stmt = text(
            f"{_COMBO_SELECT}{where_sql} {order_sql} "
            f"LIMIT :limit OFFSET :offset"
        )
        rows = (
            await conn.execute(
                list_stmt, {**params, "limit": page_size, "offset": offset}
            )
        ).all()

    # Bug.23: `total == 0` → 404. Mirrors `/cards/search` §6.1's rule so
    # the two search surfaces share one Scryfall-shaped empty contract.
    if int(total) == 0:
        return _not_found("No combos matched the query")
    data = [_row_to_combo(r) for r in rows]
    has_more = offset + len(data) < total
    return {
        "object": "list",
        "data": data,
        "total": int(total),
        "page": page,
        "page_size": page_size,
        "has_more": has_more,
    }


@router.get(
    "/{combo_id}",
    summary="Single Commander Spellbook combo",
    responses={404: _ERROR_RESPONSE},
)
async def get_combo(
    combo_id: str,
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    async with engine.connect() as conn:  # type: ignore[attr-defined]
        rows = (
            await conn.execute(
                text(f"{_COMBO_SELECT}WHERE co.id = :id"),
                {"id": combo_id},
            )
        ).all()
    if not rows:
        return _not_found(f"No combo found with id '{combo_id}'")
    return {"object": "combo", **_row_to_combo(rows[0])}
