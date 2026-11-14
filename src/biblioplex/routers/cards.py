"""Card-lookup endpoints per tech_spec.md §6.1, §6.2 and §6.4.

* ``GET /cards/search`` — DSL search, paginated 175/page (§6.1, M4).
* ``GET /cards/{card_id}`` — fetch a single card by Scryfall UUID (§6.2).
* ``GET /cards/named?exact=<name>`` — case-insensitive equality on the
  top-level ``name`` column (§6.4).

All endpoints return the original Scryfall card payload by reading the
``raw`` JSONB column verbatim — no Pydantic model wrapping. Missing rows
produce a 404 body shaped like Scryfall's error object so agent clients
can pattern-match the same way they would against api.scryfall.com.

Route order matters: ``/cards/search`` and ``/cards/named`` must be
registered **before** ``/cards/{card_id}`` so FastAPI does not attempt to
parse them as UUIDs and reject the request with 422 before dispatch.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, assert_never
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from biblioplex.classify.rules import LABELS, RULE_DESCRIPTIONS, classify_by_rules_ex
from biblioplex.decklist.resolve_pt import resolve_pt_names
from biblioplex.dsl.compiler import (
    UnsupportedOperatorError,
    _escape_ilike_value,
    compile_ast,
    compile_rank,
    text_to_numeric,
)
from biblioplex.dsl.parser import ParseError, parse
from biblioplex.errors import ErrorEnvelope
from biblioplex.errors import scryfall_error as _scryfall_error
from biblioplex.projection import ProjectionError, parse_projection, project

# OpenAPI ``responses=`` fragment for the Scryfall-shaped 4xx body. Reused
# across endpoints so the schema documents 4xx shapes (D8.2) without each
# route having to re-spell the model reference. Typed loosely (``Any``) to
# match FastAPI's accepted ``responses=`` shape — the values mix model
# refs and free-form description strings.
_ERROR_RESPONSE: dict[str, Any] = {
    "model": ErrorEnvelope,
    "description": "Scryfall-shaped error envelope.",
}


class _Engine(Protocol):
    """Subset of :class:`AsyncEngine` we exercise — only ``connect()``.

    Mirrors the pattern in :mod:`biblioplex.routers.health`: a structural
    type makes dependency-overrides trivial in tests without leaking the
    SQLAlchemy class hierarchy into test fakes.
    """

    def connect(self) -> object: ...

    def begin(self) -> Any: ...  # write path — see inventory router's _Engine


def get_read_engine(request: Request) -> _Engine:
    """Dependency: the read-pool engine bound to ``app.state.read_engine``."""
    engine: _Engine = request.app.state.read_engine
    return engine


router = APIRouter(prefix="/cards", tags=["cards"])


def _not_found(details: str) -> JSONResponse:
    """Shorthand for the 404/not_found envelope."""
    return _scryfall_error(404, "not_found", details)


def _bad_request(details: str) -> JSONResponse:
    """Shorthand for the 400/bad_request envelope (used by §6.4 D2.4)."""
    return _scryfall_error(400, "bad_request", details)


def _invalid_projection(details: str) -> JSONResponse:
    """422 envelope for ``ProjectionError`` (§7, D7.4)."""
    return _scryfall_error(422, "invalid_projection", details)


# Card and search payloads depend on user-mutable data (inventory, wishlist,
# label overrides), so there is no ETag / 304: `no-cache` + always a fresh
# body. ponytail: a validator folding a user-data version counter would
# restore 304s; the bandwidth win is negligible for a self-hosted app.
_NO_CACHE = {"Cache-Control": "no-cache"}


def _cached_json(payload: Any) -> JSONResponse:
    return JSONResponse(content=payload, headers=_NO_CACHE)


# Distinguishes "caller did not read label_reasons" (search, named, …) from
# a NULL column (single-card GET on a never-classified card).
_REASONS_UNREAD: Any = object()


def _decorate(
    raw: Any,
    predicted_labels: Any,
    label_scores: Any,
    label_reasons: Any = _REASONS_UNREAD,
) -> dict[str, Any]:
    """Inject Step-5 rules-layer fields into the Scryfall ``raw`` payload.

    Always-on: ``predicted_labels`` + ``label_scores`` ride alongside the
    Scryfall fields whether or not the caller asked for them via
    ``?projection=``. Projection-gated would force the projector to learn
    about per-row column tables (currently it only walks ``raw``); the
    bandwidth cost (~200 bytes/card) is well below the round-trip floor.

    When ``label_reasons`` is supplied, ``labels_overridden`` reports whether
    the labels are a manual correction (the reserved ``_override`` key) rather
    than the rules verdict — so the UI can flag an edited card. Callers that
    never SELECT ``label_reasons`` omit the key entirely: a ``False`` there
    would misreport an overridden card as unedited.
    """
    out = dict(raw) if raw else {}
    out["predicted_labels"] = list(predicted_labels or [])
    out["label_scores"] = dict(label_scores or {})
    if label_reasons is not _REASONS_UNREAD:
        out["labels_overridden"] = bool(
            isinstance(label_reasons, dict) and "_override" in label_reasons
        )
    return out


def _apply_projection(
    payload: dict[str, Any], projection: str | None
) -> dict[str, Any] | JSONResponse:
    """Resolve ``?projection=`` against ``payload``.

    Returns the projected dict on success, or a 422 ``JSONResponse``
    when the parser rejects the input. Empty / ``None`` projection is a
    no-op (§7 final line — wire-compat with raw Scryfall payloads).

    Centralized here so all four wired endpoints share the same parse →
    project → error-envelope pipeline.
    """
    if projection is None or not projection.strip():
        return payload
    try:
        paths = parse_projection(projection)
    except ProjectionError as exc:
        return _invalid_projection(str(exc))
    return project(payload, paths)


# Spec-mandated trigram-similarity floor for ?fuzzy=. Anything below
# this is treated as "no match" — Scryfall's own /cards/named uses the
# same heuristic so we mirror it for behavioral parity.
FUZZY_SIMILARITY_THRESHOLD = 0.4


# §6.1: hard-coded page size. Scryfall serves 175 results per page; we
# mirror that so agent clients can paginate identically.
SEARCH_PAGE_SIZE = 175

# §6.1: max query length. Bounds parser work + Postgres regex memory.
SEARCH_Q_MAX_LEN = 1000

# §6.3 /similar hybrid re-rank knobs. Vector top-K is too narrow for the
# "same effect, different wording" case (Iron Man / Repurposing Bay) —
# we over-fetch and blend six signals so substitutes rank higher than
# look-alike-but-unplayable matches (off-color, wrong CMC, wrong timing,
# wrong mechanic). Weights sum ≈ 1.0 for legibility; absolute scale
# doesn't affect ranking. Tuned via scripts/eval_similarity.py.
HYBRID_OVERFETCH = 5
HYBRID_W_VECTOR = 0.45   # pgvector cosine over embed_source
HYBRID_W_LEXICAL = 0.10  # pg_trgm similarity over oracle_search
HYBRID_W_COST = 0.10     # 1 / (1 + |Δcmc|)
HYBRID_W_COLOR = 0.15    # Jaccard over color_identity (0.5 when both colorless)
HYBRID_W_TYPE = 0.05     # 1.0 exact type_search match, 0.5 supertype, 0.0 else
HYBRID_W_TAGS = 0.15     # Jaccard over Scryfall oracle_tags (function tags)


def _hybrid_neighbours_sql(*, slot_filter: bool) -> str:
    """Build the hybrid-rerank neighbour SQL — shared by /similar and /substitutes.

    D15.8: the two endpoints previously duplicated ~99 lines of the same
    CTE + LEFT JOIN + six-signal ORDER BY. Only the candidates WHERE
    diverged by one predicate (``:slot = ANY(predicted_labels)``).
    """
    slot_clause = "    AND :slot = ANY(predicted_labels) " if slot_filter else ""
    return (
        "WITH candidates AS ("  # noqa: S608
        "  SELECT id, raw, oracle_search, cmc, color_identity, "
        "         type_search, oracle_id, predicted_labels, label_scores, "
        "         embedding <=> :target AS distance "
        "  FROM cards "
        "  WHERE id <> :id AND embedding IS NOT NULL "
        f"{slot_clause}"
        "  ORDER BY embedding <=> :target "
        "  LIMIT :overfetch"
        "), "
        "cand_tags AS ("
        "  SELECT oracle_id, array_agg(tag) AS tags "
        "  FROM oracle_tags "
        "  WHERE oracle_id IN (SELECT oracle_id FROM candidates) "
        "  GROUP BY oracle_id"
        ") "
        "SELECT c.id, c.raw, c.distance, c.predicted_labels, c.label_scores "
        "FROM candidates c "
        "LEFT JOIN cand_tags ct ON ct.oracle_id = c.oracle_id "
        "ORDER BY "
        "    :w_vector  * (1.0 - c.distance) "
        "  + :w_lexical * similarity(c.oracle_search, :target_oracle) "
        "  + :w_cost    * COALESCE(1.0 / (1.0 + abs(c.cmc - :target_cmc)), 0.0) "
        "  + :w_color   * COALESCE("
        "      cardinality(ARRAY("
        "        SELECT unnest(c.color_identity) "
        "        INTERSECT SELECT unnest(CAST(:target_ci AS text[]))"
        "      ))::float "
        "      / nullif(cardinality(ARRAY("
        "        SELECT unnest(c.color_identity) "
        "        UNION SELECT unnest(CAST(:target_ci AS text[]))"
        "      )), 0), "
        "      0.5) "
        "  + :w_type    * CASE "
        "      WHEN c.type_search = :target_type THEN 1.0 "
        "      WHEN split_part(c.type_search, ' — ', 1) "
        "         = split_part(:target_type, ' — ', 1) THEN 0.5 "
        "      ELSE 0.0 END "
        "  + :w_tags    * COALESCE("
        "      cardinality(ARRAY("
        "        SELECT unnest(ct.tags) "
        "        INTERSECT SELECT unnest(CAST(:target_tags AS text[]))"
        "      ))::float "
        "      / nullif(cardinality(ARRAY("
        "        SELECT unnest(ct.tags) "
        "        UNION SELECT unnest(CAST(:target_tags AS text[]))"
        "      )), 0), "
        "      0.0) "
        "DESC "
        "LIMIT :limit"
    )


_HYBRID_SQL_SIMILAR = _hybrid_neighbours_sql(slot_filter=False)
_HYBRID_SQL_SUBSTITUTES = _hybrid_neighbours_sql(slot_filter=True)


# D15.17: shared target-row SELECT for /similar + /substitutes. Both
# endpoints pull the same six columns (embedding + rerank inputs + the
# oracle_tags array) in one round-trip; keeping it a module constant
# stops the two branches from drifting when tag- or rerank-input surface
# changes.
_TARGET_ROW_SQL = (
    "SELECT embedding, oracle_search, cmc, color_identity, type_search, "
    "       COALESCE(("
    "         SELECT array_agg(tag) FROM oracle_tags "
    "         WHERE oracle_id = c.oracle_id"
    "       ), ARRAY[]::text[]) AS oracle_tags "
    "FROM cards c WHERE c.id = :id"
)


async def _fetch_similar_target(
    conn: Any, card_id: UUID
) -> tuple[Any, str, Any, list[str], str, list[str]] | JSONResponse:
    """Shared target-row fetch for /similar + /substitutes.

    Returns the six-tuple ``(embedding, oracle_search, cmc, color_identity,
    type_search, oracle_tags)`` on hit, or a ready-to-return ``_not_found``
    envelope when the card is unknown OR present-but-still-un-embedded
    (both surface as 404 per §6.3 / §6.8).

    Kept as a hit-or-envelope union rather than raising because the
    caller's outer ``engine.connect()`` block still needs to fire the
    neighbour SELECT after the fast-path exit — an exception would
    unwind the conn ctx early.
    """
    target_result = await conn.execute(
        text(_TARGET_ROW_SQL), {"id": str(card_id)},
    )
    target_row = target_result.one_or_none()
    if target_row is None or target_row.embedding is None:
        return _not_found(f"No card found with id '{card_id}'")
    return (
        target_row.embedding,
        target_row.oracle_search or "",
        target_row.cmc,
        list(target_row.color_identity or []),
        target_row.type_search or "",
        list(target_row.oracle_tags or []),
    )


def _serialize_neighbours(
    rows: Any,
    projection_paths: list[list[str | int]] | None,
) -> list[dict[str, Any]]:
    """Decorate → inject similarity → optionally project each neighbour row.

    Shared row-loop tail for /similar + /substitutes. Both endpoints
    decorate with Step-5 predicted_labels/label_scores BEFORE injecting
    ``similarity`` so callers projecting ``similarity`` see the live
    value alongside the rules-layer fields.
    """
    data: list[dict[str, Any]] = []
    for row in rows:
        entry = _decorate(row["raw"], row["predicted_labels"], row["label_scores"])
        entry["similarity"] = 1.0 - float(row["distance"])
        if projection_paths is not None:
            entry = project(entry, projection_paths)
        data.append(entry)
    return data


def _build_cards_table() -> sa.Table:
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
        sa.Column("raw", postgresql.JSONB),
    )


_CARDS = _build_cards_table()


class SearchOrder(StrEnum):
    """``order=`` enum per §6.1.

    ``usd`` extends the documented §6.1 enum (`name|cmc|power|toughness|
    released|color|edhrec|rarity`) — see drift §6.1 in todo.md. Routed to
    ``cards.price_usd`` (D1.9: MIN(prices.usd) across an oracle's printings).

    ``relevance`` (D11.1) sorts by trigram word-similarity between the
    query's bare-word terms and ``cards.name_search`` (DESC, best match
    first). Falls back to ``name_sort_key`` ASC when the query has no
    bare-word terms (filter-only queries like ``t:legendary``).
    """

    name = "name"
    cmc = "cmc"
    power = "power"
    toughness = "toughness"
    released = "released"
    color = "color"
    edhrec = "edhrec"
    usd = "usd"
    rarity = "rarity"
    relevance = "relevance"


class SearchDir(StrEnum):
    """``dir=`` enum per §6.1."""

    auto = "auto"
    asc = "asc"
    desc = "desc"


class SearchUnique(StrEnum):
    """``unique=`` enum per §6.1.

    All three values are accepted, but ``prints`` and ``art`` silently
    alias to ``cards``: we already dedupe one row per ``oracle_id`` at
    ingest (§1.1), so the three modes are equivalent against this schema.
    """

    cards = "cards"
    prints = "prints"
    art = "art"


# Rarity rank mirrors D3.10 in dsl.compiler — ordered:
# common(0) < uncommon(1) < rare(2) < special(3) < mythic(4) < bonus(5).
_RARITY_RANK: dict[str, int] = {
    "common": 0,
    "uncommon": 1,
    "rare": 2,
    "special": 3,
    "mythic": 4,
    "bonus": 5,
}

# `dir=auto` per-order default direction.
#   asc:  name, rarity, color, cmc, power, toughness, edhrec
#   desc: released, usd
# Rationale (D4.6):
#   * `edhrec` is asc because Scryfall's `order=edhrec` defaults to
#     rank 1 (most popular) first, and rank 1 IS the smallest int.
#     This flips the pre-D1.9 fallback behaviour (which defaulted desc
#     when there was no real column yet) — see §6.1 drift resolution.
#   * `usd` is desc because Scryfall's `order=usd` defaults to most
#     expensive first; cheap cards are the long tail, not the headline.
_AUTO_DIR: dict[SearchOrder, SearchDir] = {
    SearchOrder.name: SearchDir.asc,
    SearchOrder.cmc: SearchDir.asc,
    SearchOrder.power: SearchDir.asc,
    SearchOrder.toughness: SearchDir.asc,
    SearchOrder.color: SearchDir.asc,
    SearchOrder.rarity: SearchDir.asc,
    SearchOrder.edhrec: SearchDir.asc,
    SearchOrder.released: SearchDir.desc,
    SearchOrder.usd: SearchDir.desc,
    SearchOrder.relevance: SearchDir.desc,
}


def _order_expression(
    order: SearchOrder, rank: sa.ColumnElement[float] | None = None
) -> sa.ColumnElement[Any]:
    """Return the primary sort expression for the given ``order=`` mode.

    * ``power``/``toughness``: numeric-cast guarded by a regex so values
      like ``*`` and ``X`` sort as NULL (we wrap NULLS LAST below so they
      sink consistently regardless of asc/desc — Postgres' default would
      otherwise flip nulls-first on DESC).
    * ``rarity``: CASE-mapped rank 0..5 so the ordered comparator from
      §5.1 / D3.10 stays consistent between filter and sort.
    * ``color``: ``cards.color_sort_key`` — canonical WUBRG-cycle position
      0–31 (colorless, mono WUBRG, 2c allied, 2c enemy, shards, wedges,
      4c, 5c). Computed in ``ingest/color_sort.py``.
    * ``edhrec``: ``cards.edhrec_rank`` (D1.9). Rank 1 = most popular.
    * ``usd``: ``cards.price_usd`` (D1.9, MIN over an oracle's printings).
      Extends §6.1's documented enum — drift logged.
    * ``relevance`` (D11.1): the ``word_similarity`` expression returned
      by ``compile_rank``. When ``rank is None`` (filter-only query with
      no bare-word terms), falls back to ``name_sort_key`` so a search
      like ``q=t:elf&order=relevance`` still returns a stable result set
      instead of a 500.
    """
    if order is SearchOrder.name:
        return _CARDS.c.name_sort_key
    if order is SearchOrder.cmc:
        return _CARDS.c.cmc
    if order is SearchOrder.power:
        return text_to_numeric(_CARDS.c.power)
    if order is SearchOrder.toughness:
        return text_to_numeric(_CARDS.c.toughness)
    if order is SearchOrder.released:
        return _CARDS.c.released_at
    if order is SearchOrder.color:
        return _CARDS.c.color_sort_key
    if order is SearchOrder.rarity:
        return sa.case(_RARITY_RANK, value=_CARDS.c.rarity)
    if order is SearchOrder.edhrec:
        return _CARDS.c.edhrec_rank
    if order is SearchOrder.usd:
        return _CARDS.c.price_usd
    if order is SearchOrder.relevance:
        return rank if rank is not None else _CARDS.c.name_sort_key
    assert_never(order)


def _resolve_dir(order: SearchOrder, direction: SearchDir) -> SearchDir:
    """Resolve ``dir=auto`` against the per-order default."""
    if direction is SearchDir.auto:
        return _AUTO_DIR[order]
    return direction


@router.get(
    "/search",
    summary="DSL search over the card corpus",
    description=(
        "Compiles the §5 DSL query into SQL against the `cards` table, "
        "paginated 175/page, and returns a Scryfall `List` envelope. "
        "`order=` selects the primary sort column; `dir=auto` resolves "
        "to `desc` for `released`/`usd` and `asc` otherwise. The primary "
        "expression is wrapped NULLS LAST so unranked/unpriced/non-numeric "
        "rows always sink. `id ASC` is the secondary tie-break for stable "
        "pagination."
    ),
    responses={404: _ERROR_RESPONSE, 400: _ERROR_RESPONSE, 422: _ERROR_RESPONSE},
)
async def search_cards(
    request: Request,
    response: Response,
    q: str | None = Query(
        default=None,
        description="DSL query per §5. Required; empty/whitespace → 422.",
        examples=["t:dragon c:r", "o:flying pow>=5"],
    ),
    unique: SearchUnique = Query(
        default=SearchUnique.cards,
        description="Accepted for Scryfall compatibility; all values alias to `cards`.",
    ),
    order: SearchOrder = Query(
        default=SearchOrder.name,
        description="Primary sort column.",
        examples=["name", "cmc"],
    ),
    dir: SearchDir = Query(
        default=SearchDir.auto,
        description="Sort direction; `auto` resolves per `order=`.",
    ),
    page: int = Query(
        default=1,
        ge=1,
        description="1-indexed page number; 175 results per page.",
        examples=[1],
    ),
    format: str | None = Query(
        default=None,
        description="JSON-only; `csv`/`text` → 400.",
    ),
    fields: str | None = Query(
        default=None,
        description=(
            "Comma-separated dotted-path projection per §7. Shorter alias "
            "for `?projection=` — both names accept identical CSV syntax; "
            "when both are supplied, `fields` wins (D15.29)."
        ),
        examples=["name,cmc,colors"],
    ),
    projection: str | None = Query(
        default=None,
        include_in_schema=False,
    ),
    count: bool = Query(
        default=True,
        description=(
            "When `false`, skip the `COUNT(*) OVER ()` window — the "
            "response's `total_cards` is `null` and `has_more` is "
            "derived from `len(data) == 175`. Useful for agents "
            "walking `next_page` links that don't need the running "
            "total on every request. Empty result sets do not 404 "
            "under `count=false` — the 404 rule only applies when "
            "`total_cards` is known to be zero."
        ),
    ),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Run a DSL search against ``cards`` per §6.1 (M4 D4.1–D4.5).

    Pipeline:
      1. Validate ``q`` (presence + ≤1000 chars) and ``format`` (JSON-only).
      2. Parse ``q`` through the DSL ``parse()``.
      3. Compile the AST to a SQLAlchemy WHERE via ``compile_ast``.
      4. ``SELECT raw, COUNT(*) OVER () AS total FROM cards WHERE … ORDER BY
         <order_expr> <dir>, id ASC OFFSET (page-1)*175 LIMIT 175``.
      5. Wrap rows in the Scryfall ``List`` envelope.

    ``unique={cards,prints,art}`` is accepted for Scryfall compatibility
    but all three values behave the same — we already dedupe one row per
    ``oracle_id`` at ingest (§1.1), so there is no distinct "prints" or
    "art" variant to surface. Any other value is rejected with 422 by the
    enum validator.

    Error mapping:
      * Empty / missing / whitespace-only ``q`` → 422 ``q is required``.
      * ``q`` > 1000 chars → 422 ``q too long``.
      * User ``q`` ≤ 1000 chars but the ``SEARCH_DEFAULT_FILTER`` wrap
        pushes the rendered query past 1000 chars → 422 with a
        filter-aware ``details`` message naming ``SEARCH_DEFAULT_FILTER``
        so operators can see the deployment config is the cause.
      * ``format=csv|text`` → 400 ``unsupported format`` (JSON-only).
      * ``ParseError`` (bad DSL) → 422 with the parser message.
      * ``UnsupportedOperatorError`` (§5.2 keys) → 422 ``unsupported
        operator: <key>``.
      * ``total_cards == 0`` → 404 per §6.1 final line.
    """
    # `unique` is accepted for compatibility but is a no-op here — see
    # docstring + D4.5. Bind to `_` so linters know we read it.
    _ = unique
    # D15.29 — `?fields=` is a shorter alias for `?projection=`. Both accept
    # identical CSV syntax; when both are supplied, `fields` wins.
    projection = fields if fields is not None else projection
    # Format gate runs before anything else — §6.1 promises 400, not 422.
    if format is not None and format.lower() not in ("json", ""):
        return _scryfall_error(400, "unsupported_format", "unsupported format")
    # q presence + length bounds. Whitespace-only counts as empty.
    if q is None or not q.strip():
        return _scryfall_error(422, "bad_request", "q is required")
    # Pre-wrap cap on user input. Fires first so an oversized client `q`
    # gets the plain "q too long" message regardless of deployment config.
    if len(q) > SEARCH_Q_MAX_LEN:
        return _scryfall_error(422, "bad_request", "q too long")

    # Optional deployment-wide filter AND-ed onto every query. User q is
    # wrapped in parens so DSL `or` inside it still binds before the AND.
    # Surface it via `X-Applied-Filter` so agents/operators see the
    # deployment-side shadow without having to diff `q` against the raw url.
    # D16.6: read the pre-stripped cached form from app.state — no
    # per-request `.strip()` on the hot search path.
    default_filter = request.app.state.search_default_filter
    if default_filter:
        response.headers["X-Applied-Filter"] = default_filter
        q = f"({q}) {default_filter}"
        # Post-wrap re-check: the user q passed the cap above, but the
        # deployment filter inflated the rendered query past the bound.
        # Distinct message so operators can see the deployment config is
        # the cause, not the client. Status + code unchanged per §6.1.
        if len(q) > SEARCH_Q_MAX_LEN:
            return _scryfall_error(
                422,
                "bad_request",
                f"q too long after SEARCH_DEFAULT_FILTER wrap "
                f"({len(q)} > {SEARCH_Q_MAX_LEN})",
            )

    # ParseError / UnsupportedOperatorError both surface as 422 per §6.1.
    try:
        ast = parse(q)
        where = compile_ast(ast, _CARDS)
        # `rank` is None when the query has no bare-word terms (filter-only).
        # `_order_expression` then falls back to `name_sort_key` for
        # `order=relevance`, and ignores `rank` entirely for every other order.
        rank = compile_rank(ast, _CARDS) if order is SearchOrder.relevance else None
    except UnsupportedOperatorError as exc:
        return _scryfall_error(422, "unsupported_operator", str(exc))
    except (ParseError, ValueError) as exc:
        return _scryfall_error(422, "bad_request", str(exc))

    # `order=relevance` with no bare-word terms falls back to `name`'s sort.
    # Resolve `dir=auto` against the *effective* order so the fallback gets
    # `asc` (matching `order=name`), not `desc` (relevance's default).
    effective_order = order
    if order is SearchOrder.relevance and rank is None:
        effective_order = SearchOrder.name
    primary = _order_expression(order, rank)
    resolved_dir = _resolve_dir(effective_order, dir)
    # NULLS LAST on every order so unranked/unpriced/non-numeric (power=*/X)
    # cards always sink. Postgres' default is nulls-LAST on ASC but
    # nulls-FIRST on DESC; wrapping explicitly makes the two new orders
    # (edhrec, usd) consistent regardless of dir=, and tightens the
    # existing power/toughness/numeric-cast cases too.
    primary_directional = (
        primary.desc() if resolved_dir is SearchDir.desc else primary.asc()
    )
    primary_clause = sa.nulls_last(primary_directional)

    offset = (page - 1) * SEARCH_PAGE_SIZE
    # D15.15: `count=false` drops the `COUNT(*) OVER ()` window so
    # filter-only queries (`t:legendary`) don't pay a scan-and-count on
    # every page. Total is reported as `null` in the response; agents
    # walking `next_page` links use `has_more = len(data) == 175` to
    # decide whether to keep paginating.
    select_cols = [
        _CARDS.c.raw.label("raw"),
        _CARDS.c.predicted_labels.label("predicted_labels"),
        _CARDS.c.label_scores.label("label_scores"),
    ]
    if count:
        select_cols.append(sa.func.count().over().label("total"))
    stmt = (
        sa.select(*select_cols)
        .where(where)
        .order_by(primary_clause, _CARDS.c.id.asc())
        .offset(offset)
        .limit(SEARCH_PAGE_SIZE)
    )

    async with engine.connect() as conn:  # type: ignore[attr-defined]
        result = await conn.execute(stmt)
        rows = result.mappings().all()

    # `total_cards` comes from the COUNT(*) OVER () window column on any
    # returned row. When the result set is empty (either no matches OR an
    # out-of-range `page` past the last full page), fall back to 0 and let
    # §6.1's "total_cards == 0 → 404" rule cover it. Under `count=false`
    # we lack the total — an empty page returns an empty envelope with
    # `total_cards=null` instead of 404 so agents can keep the page walk
    # deterministic regardless of the count knob.
    if count:
        total: int | None = int(rows[0]["total"]) if rows else 0
        if total == 0:
            return _not_found(f"No cards matched '{q}'")
    else:
        total = None

    # Parse projection once per request; ProjectionError → 422 short-circuit
    # before we serialize a single row.
    projection_paths: list[list[str | int]] | None
    if projection is not None and projection.strip():
        try:
            projection_paths = parse_projection(projection)
        except ProjectionError as exc:
            return _invalid_projection(str(exc))
    else:
        projection_paths = None

    decorated = [
        _decorate(row["raw"], row["predicted_labels"], row["label_scores"])
        for row in rows
    ]
    if projection_paths is None:
        data: list[dict[str, Any]] = decorated
    else:
        data = [project(entry, projection_paths) for entry in decorated]

    if total is not None:
        has_more = total > page * SEARCH_PAGE_SIZE
    else:
        # D15.15 `count=false`: a full page means there might be more.
        has_more = len(data) == SEARCH_PAGE_SIZE
    body: dict[str, Any] = {
        "object": "list",
        "total_cards": total,
        "has_more": has_more,
        "data": data,
    }
    if has_more:
        body["next_page"] = str(request.url.include_query_params(page=page + 1))
    # Same `response` object as the X-Applied-Filter branch above.
    response.headers["Cache-Control"] = "no-cache"
    return body


AUTOCOMPLETE_LIMIT = 20
AUTOCOMPLETE_Q_MAX_LEN = 100


@router.get(
    "/autocomplete",
    summary="Prefix + trigram name autocomplete",
    description=(
        "Returns up to 20 card names matching `q` — prefix hits (case-"
        "insensitive) rank first, then pg_trgm word-similarity for typo "
        "tolerance. Scryfall-shaped `catalog` envelope. Empty `q` → 422."
    ),
    responses={422: _ERROR_RESPONSE},
)
async def autocomplete_cards(
    q: str | None = Query(
        default=None,
        description="Query fragment; ≤100 chars.",
        examples=["light", "sol r"],
    ),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Prefix-first, trigram-fallback name autocomplete."""
    if q is None or not q.strip():
        return _scryfall_error(422, "bad_request", "q is required")
    if len(q) > AUTOCOMPLETE_Q_MAX_LEN:
        return _scryfall_error(422, "bad_request", "q too long")
    q_stripped = q.strip()
    # Bug.16: escape ILIKE metachars in the user q before appending the
    # trailing `%` — otherwise `foo_bar` / `50%` treat `_` / `%` as
    # wildcards and match unrelated rows. Trigram (`%>`) side is left
    # verbatim; pg_trgm has no metachars.
    async with engine.connect() as conn:  # type: ignore[attr-defined]
        result = await conn.execute(
            text(
                "SELECT name FROM cards "
                "WHERE LOWER(name) LIKE LOWER(:prefix) "
                "   OR name_sort_key %> :q "
                "ORDER BY (LOWER(name) LIKE LOWER(:prefix)) DESC, "
                "         word_similarity(name_sort_key, :q) DESC, "
                "         name_sort_key ASC "
                "LIMIT :limit"
            ),
            {
                "prefix": _escape_ilike_value(q_stripped) + "%",
                "q": q_stripped,
                "limit": AUTOCOMPLETE_LIMIT,
            },
        )
        names = [r[0] for r in result.all()]
    return {
        "object": "catalog",
        "total_values": len(names),
        "data": names,
    }


@router.get(
    "/named",
    summary="Single-card lookup by name (exact or fuzzy)",
    description=(
        "Exactly one of `exact` or `fuzzy` must be supplied. `exact` does "
        "case-insensitive equality on top-level `name`; `fuzzy` runs a "
        "pg_trgm best-match against `name_sort_key` and accepts the top "
        "row when `similarity >= 0.4` (Scryfall's documented threshold)."
    ),
    responses={404: _ERROR_RESPONSE, 400: _ERROR_RESPONSE, 422: _ERROR_RESPONSE},
)
async def get_card_by_name(
    exact: str | None = Query(
        default=None,
        description="Case-insensitive exact match on top-level `name`.",
        examples=["Lightning Bolt"],
    ),
    fuzzy: str | None = Query(
        default=None,
        description="pg_trgm best-match against `name_sort_key`; threshold 0.4.",
        examples=["lightning blot"],
    ),
    fields: str | None = Query(
        default=None,
        description=(
            "Comma-separated dotted-path projection per §7. Shorter alias "
            "for `?projection=` — both names accept identical CSV syntax; "
            "when both are supplied, `fields` wins (D15.29)."
        ),
        examples=["name,mana_cost"],
    ),
    projection: str | None = Query(
        default=None,
        include_in_schema=False,
    ),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Look up a single card by name per §6.4.

    Modes:

    * ``?exact=<name>`` — case-insensitive equality on the top-level
      ``cards.name`` column (``LOWER(name) = LOWER(:name)``). Pushing
      case-folding to Postgres keeps locale/Unicode semantics consistent
      with the rest of the search surface.
    * ``?fuzzy=<name>`` — pg_trgm similarity against ``name_sort_key``,
      returning the single best match if ``similarity >= 0.4`` (spec-
      mandated floor). Sorting on the score and ``LIMIT 1`` lets the DB
      do the picking; the router just unwraps ``raw`` from the row.

    Exactly one of ``exact``/``fuzzy`` is required (D2.4); supplying
    neither, or both, returns 400 with a Scryfall-shaped ``bad_request``
    body before touching the database. 404 (Scryfall-shaped) when the
    chosen mode yields no match.
    """
    # D15.29 — `?fields=` alias wins when both supplied.
    projection = fields if fields is not None else projection
    # D2.4 guard — must short-circuit before any connect() so callers
    # get a deterministic 400 instead of a DB round-trip that ignores
    # one of the params.
    if (exact is None) == (fuzzy is None):
        return _bad_request("exactly one of exact|fuzzy required")

    if exact is not None:
        async with engine.connect() as conn:  # type: ignore[attr-defined]
            result = await conn.execute(
                text(
                    "SELECT oracle_id, raw, predicted_labels, label_scores "
                    "FROM cards WHERE LOWER(name) = LOWER(:name)"
                ),
                {"name": exact},
            )
            row = result.one_or_none()
        if row is None:
            return _not_found(f"No card found with name '{exact}'")
        payload = _decorate(row.raw, row.predicted_labels, row.label_scores)
        projected = _apply_projection(payload, projection)
        if isinstance(projected, JSONResponse):
            return projected
        return _cached_json(projected)

    # `fuzzy is not None` is implied by the XOR guard at the top.
    async with engine.connect() as conn:  # type: ignore[attr-defined]
        result = await conn.execute(
            text(
                "SELECT oracle_id, raw, predicted_labels, label_scores FROM cards "
                "WHERE similarity(name_sort_key, LOWER(:name)) >= :threshold "
                "ORDER BY similarity(name_sort_key, LOWER(:name)) DESC "
                "LIMIT 1"
            ),
            {"name": fuzzy, "threshold": FUZZY_SIMILARITY_THRESHOLD},
        )
        row = result.one_or_none()
    if row is None:
        return _not_found(f"No card found matching '{fuzzy}'")
    payload = _decorate(row.raw, row.predicted_labels, row.label_scores)
    projected = _apply_projection(payload, projection)
    if isinstance(projected, JSONResponse):
        return projected
    return _cached_json(projected)


@router.get(
    "/{card_id}/similar",
    summary="Top-K nearest cards by embedding cosine distance",
    description=(
        "Returns the cards closest to `:card_id` in embedding space, "
        "ordered by pgvector cosine distance (`<=>`). Each entry's "
        "`raw` payload is decorated with `similarity = 1 - distance`. "
        "Returns 404 when the target card is missing or has no embedding yet."
    ),
    responses={404: _ERROR_RESPONSE, 422: _ERROR_RESPONSE},
)
async def get_similar_cards(
    card_id: UUID,
    limit: int = Query(
        default=20,
        ge=1,
        le=100,
        description="Number of neighbours to return; 1-100, default 20.",
        examples=[5, 20],
    ),
    fields: str | None = Query(
        default=None,
        description=(
            "Comma-separated dotted-path projection per §7. Shorter alias "
            "for `?projection=` — both names accept identical CSV syntax; "
            "when both are supplied, `fields` wins (D15.29)."
        ),
        examples=["name,similarity"],
    ),
    projection: str | None = Query(
        default=None,
        include_in_schema=False,
    ),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Top-K vector-similar cards per §6.3.

    Two-query path: (1) fetch the target row's ``embedding`` via its
    primary key; (2) order the rest of ``cards`` by cosine distance
    (pgvector ``<=>``) against that vector, excluding the target itself
    and any rows whose embedding is still NULL (un-embedded yet).

    Pre-fetching the target embedding (instead of an inline correlated
    sub-select) lets us distinguish "card missing" from "card present
    but un-embedded" cleanly — both legitimately surface as a 404 per
    §6.3, but keeping them separate in code makes the failure mode
    obvious to whoever debugs the next strange empty response.

    Response wraps the rows in the Scryfall ``List`` envelope and
    injects ``similarity = 1 - distance`` into each entry's ``raw``
    payload (matching Scryfall's convention of decorating list entries
    with endpoint-specific fields). ``has_more``/``next_page`` are
    always ``false``/``null`` — similar has no pagination, the SQL
    ``LIMIT`` is the only bound.

    ``total_cards`` mirrors ``len(data)`` since ``LIMIT`` already
    truncates the result; we don't promise the global neighbour count
    (drift §6.3).
    """
    # D15.29 — `?fields=` alias wins when both supplied.
    projection = fields if fields is not None else projection
    # Step 1 — target row lookup (D15.17 shared helper: pulls every column
    # the rerank reads, plus the target's oracle_tags as a text[] via
    # inline subquery, in one round-trip).
    async with engine.connect() as conn:  # type: ignore[attr-defined]
        target = await _fetch_similar_target(conn, card_id)
        if isinstance(target, JSONResponse):
            return target
        target_vec, target_oracle, target_cmc, target_ci, target_type, target_tags = target

        # Step 2 — vector-narrow + structured rerank.
        # Over-fetch by ``HYBRID_OVERFETCH`` so the rerank has room to pull
        # up cards where one signal is strong but the embedding alone
        # wouldn't put them in the top-K (off-color reword, modal X cost,
        # utility-land flavor twins). The HNSW index handles the inner
        # vector ORDER BY; the outer SELECT then blends six signals:
        # cosine + lexical (pg_trgm) + cost proximity + color-identity
        # Jaccard + type match + oracle-tag Jaccard.
        #
        # Tag Jaccard joins a `cand_tags` CTE that aggregates oracle_tags
        # only for the over-fetched candidate set (~100 rows) instead of
        # the whole table — EXPLAIN-confirmed cheaper than an inline
        # correlated subquery in the ORDER BY.
        #
        # Color Jaccard via ARRAY(... INTERSECT/UNION ...) because text[]
        # has no native intersect operator (intarray is int4 only). The
        # COALESCE → 0.5 floor on color fires only when both arrays are
        # empty (both cards colorless) so colorless artifacts don't ride
        # along as "matches every color"; for tags the floor is 0.0
        # (missing-tags is not informative).
        #
        # Type match uses `' — '` as the supertype/subtype split — that's
        # the Scryfall canonical em-dash separator preserved in type_search.
        neighbours_result = await conn.execute(
            text(_HYBRID_SQL_SIMILAR),
            {
                "target": target_vec,
                "target_oracle": target_oracle,
                "target_cmc": target_cmc,
                "target_ci": target_ci,
                "target_type": target_type,
                "target_tags": target_tags,
                "id": str(card_id),
                "limit": limit,
                "overfetch": limit * HYBRID_OVERFETCH,
                "w_vector": HYBRID_W_VECTOR,
                "w_lexical": HYBRID_W_LEXICAL,
                "w_cost": HYBRID_W_COST,
                "w_color": HYBRID_W_COLOR,
                "w_type": HYBRID_W_TYPE,
                "w_tags": HYBRID_W_TAGS,
            },
        )
        rows = neighbours_result.mappings().all()

    # Parse projection once; ProjectionError → 422 short-circuit before
    # we annotate/serialize any rows. `similarity` is in the whitelist so
    # callers can project it just like any other top-level key.
    projection_paths: list[list[str | int]] | None
    if projection is not None and projection.strip():
        try:
            projection_paths = parse_projection(projection)
        except ProjectionError as exc:
            return _invalid_projection(str(exc))
    else:
        projection_paths = None

    # D15.17 shared row-loop tail: decorate → inject similarity → optionally project.
    data = _serialize_neighbours(rows, projection_paths)

    return {
        "object": "list",
        "total_cards": len(data),
        "has_more": False,
        "next_page": None,
        "data": data,
    }


@router.get(
    "/{card_id}/substitutes",
    summary="Slot-constrained top-K similar cards",
    description=(
        "Hybrid nearest-neighbour identical to `/similar` (§6.3), filtered "
        "to candidates whose `predicted_labels` contains the requested "
        "`slot` (e.g. `slot=ramp`). Use this to swap a single card while "
        "preserving its functional role in the deck. `slot` must be one "
        "of the LABELS slugs; unknown slugs return 422. See §6.8."
    ),
    responses={404: _ERROR_RESPONSE, 422: _ERROR_RESPONSE},
)
async def get_substitutes(
    card_id: UUID,
    slot: str = Query(
        ...,
        description="Predicted-label slug; one of the LABELS tuple.",
        examples=["ramp", "card_advantage"],
    ),
    limit: int = Query(default=20, ge=1, le=100, examples=[5, 20]),
    fields: str | None = Query(
        default=None,
        description=(
            "Comma-separated dotted-path projection per §7. Shorter alias "
            "for `?projection=` — both names accept identical CSV syntax; "
            "when both are supplied, `fields` wins (D15.29)."
        ),
        examples=["name,similarity"],
    ),
    projection: str | None = Query(
        default=None,
        include_in_schema=False,
    ),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Slot-constrained variant of /similar per §6.8.

    Reuses the same hybrid rerank as /similar; only the candidate CTE
    gains ``AND :slot = ANY(predicted_labels)`` so over-fetch already
    sees a slot-pure pool. SQL is intentionally duplicated against the
    /similar query for legibility — share if a third variant lands.
    """
    if slot not in LABELS:
        return _scryfall_error(
            422, "unsupported_operator", f"unknown slot: {slot}"
        )

    # D15.29 — `?fields=` alias wins when both supplied.
    projection = fields if fields is not None else projection
    async with engine.connect() as conn:  # type: ignore[attr-defined]
        # D15.17 shared target-row fetch (same six-tuple as /similar).
        target = await _fetch_similar_target(conn, card_id)
        if isinstance(target, JSONResponse):
            return target
        target_vec, target_oracle, target_cmc, target_ci, target_type, target_tags = target

        neighbours_result = await conn.execute(
            text(_HYBRID_SQL_SUBSTITUTES),
            {
                "target": target_vec,
                "target_oracle": target_oracle,
                "target_cmc": target_cmc,
                "target_ci": target_ci,
                "target_type": target_type,
                "target_tags": target_tags,
                "id": str(card_id),
                "slot": slot,
                "limit": limit,
                "overfetch": limit * HYBRID_OVERFETCH,
                "w_vector": HYBRID_W_VECTOR,
                "w_lexical": HYBRID_W_LEXICAL,
                "w_cost": HYBRID_W_COST,
                "w_color": HYBRID_W_COLOR,
                "w_type": HYBRID_W_TYPE,
                "w_tags": HYBRID_W_TAGS,
            },
        )
        rows = neighbours_result.mappings().all()

    projection_paths: list[list[str | int]] | None
    if projection is not None and projection.strip():
        try:
            projection_paths = parse_projection(projection)
        except ProjectionError as exc:
            return _invalid_projection(str(exc))
    else:
        projection_paths = None

    # D15.17 shared row-loop tail.
    data = _serialize_neighbours(rows, projection_paths)

    return {
        "object": "list",
        "total_cards": len(data),
        "has_more": False,
        "next_page": None,
        "data": data,
    }


@router.get(
    "/{card_id}/tags",
    summary="Oracle tags (Scryfall Tagger) for a card",
    description=(
        "Returns the list of Scryfall oracle-tag slugs associated with this "
        "card's `oracle_id`. Tag hierarchy is flattened at ingest, so the "
        "list includes every ancestor tag whose subtree contains this card "
        "(matching `otag:` semantics). 404 when the card is unknown."
    ),
    responses={404: _ERROR_RESPONSE, 422: _ERROR_RESPONSE},
)
async def get_card_tags(
    card_id: UUID,
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """List oracle tags for ``card_id``.

    Single statement: resolve ``id → oracle_id`` and join against
    ``oracle_tags``. Empty list when the card exists but has no tags;
    404 when the card itself is unknown so the UI can distinguish "card
    has no tags" from "stale id".
    """
    async with engine.connect() as conn:  # type: ignore[attr-defined]
        result = await conn.execute(
            text(
                "SELECT c.oracle_id, ot.tag "
                "FROM cards c "
                "LEFT JOIN oracle_tags ot ON ot.oracle_id = c.oracle_id "
                "WHERE c.id = :id "
                "ORDER BY ot.tag"
            ),
            {"id": str(card_id)},
        )
        rows = result.all()
    if not rows:
        return _not_found(f"No card found with id '{card_id}'")
    tags = [r[1] for r in rows if r[1] is not None]
    return _cached_json({"object": "list", "data": tags})


@router.get(
    "/{card_id}/combos",
    summary="Commander Spellbook combos containing this card (D14.2)",
    description=(
        "Returns every combo whose `oracle_ids` array contains this card's "
        "`oracle_id`. Each entry carries the combo id, the resolved card "
        "names (in the combo's required order), and the source's "
        "`prereqs` / `steps` / `results` fields. Empty `data` when the "
        "card exists but no ingested combo references it (e.g. a stock "
        "deployment with `COMBOS_SOURCE_URL` unset). 404 when the card "
        "itself is unknown."
    ),
    responses={404: _ERROR_RESPONSE, 422: _ERROR_RESPONSE},
)
async def get_card_combos(
    card_id: UUID,
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """List Commander Spellbook combos containing ``card_id``.

    Single statement: LEFT JOIN cards → combos on the array-membership
    predicate + a LATERAL name-resolution subquery so the response
    surfaces card names (nicer for direct consumers) rather than raw
    ``oracle_id`` uuids. The LEFT JOIN ensures we get one row even when
    the card has no combos so we can distinguish "exists, no combos"
    (empty data) from "unknown card" (404).
    """
    # Bug.26: name + id aggregation share ONE LATERAL subquery. Prior
    # shape fired two nearly-identical scalar subqueries walking the
    # same `unnest(oracle_ids) WITH ORDINALITY` — doubled the per-row
    # planner work for zero information gain. The LATERAL runs once
    # per combo row and projects both aggregates.
    async with engine.connect() as conn:  # type: ignore[attr-defined]
        result = await conn.execute(
            text(
                "SELECT c.oracle_id, "
                "       co.id AS combo_id, "
                "       co.prereqs, "
                "       co.steps, "
                "       co.results, "
                "       resolved.card_names, "
                "       resolved.card_ids "
                "FROM cards c "
                "LEFT JOIN combos co ON c.oracle_id = ANY(co.oracle_ids) "
                "LEFT JOIN LATERAL ( "
                "  SELECT array_agg(cd.name ORDER BY t.ord) AS card_names, "
                "         array_agg(cd.id::text ORDER BY t.ord) AS card_ids "
                "  FROM unnest(COALESCE(co.oracle_ids, ARRAY[]::uuid[])) "
                "       WITH ORDINALITY t(oid, ord) "
                "  JOIN cards cd ON cd.oracle_id = t.oid "
                ") resolved ON true "
                "WHERE c.id = :id "
                "ORDER BY co.id NULLS FIRST"
            ),
            {"id": str(card_id)},
        )
        rows = result.all()
    if not rows:
        return _not_found(f"No card found with id '{card_id}'")
    data = [
        {
            "id": r.combo_id,
            "cards": list(r.card_names or []),
            "card_ids": list(r.card_ids or []),
            "prereqs": r.prereqs,
            "steps": r.steps,
            "results": list(r.results or []),
        }
        for r in rows
        if r.combo_id is not None
    ]
    return _cached_json({"object": "list", "data": data})


@router.get(
    "/{card_id}/explain",
    summary="Predicted-label explainability (D15.3)",
    description=(
        "Returns the per-label rules-classifier verdict for a card: for "
        "each of the five template labels, the score in `[0, 1]` plus the "
        "list of rule ids that fired (with human-readable descriptions). "
        "Sourced from `label_scores` + `label_reasons` written by the "
        "rules-layer classifier during refresh. 404 when the card is unknown."
    ),
    responses={404: _ERROR_RESPONSE, 422: _ERROR_RESPONSE},
)
async def get_card_explain(
    card_id: UUID,
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Explain the rules-classifier verdict for ``card_id``.

    Response envelope::

        {
          "object": "explain",
          "labels": {
            "<label>": {"score": <float>, "rules": [{"id": "<slug>", "why": "..."}]}
          }
        }

    Empty `rules` list is legal (e.g. `plan` is a residue, never a rule
    hit). Unknown rule ids in `label_reasons` fall back to the slug for
    `why` so a schema mismatch is visible rather than fatal.
    """
    async with engine.connect() as conn:  # type: ignore[attr-defined]
        result = await conn.execute(
            text(
                "SELECT oracle_id, label_scores, label_reasons "
                "FROM cards WHERE id = :id"
            ),
            {"id": str(card_id)},
        )
        row = result.one_or_none()
    if row is None:
        return _not_found(f"No card found with id '{card_id}'")
    scores = row.label_scores or {}
    reasons = row.label_reasons or {}
    labels: dict[str, Any] = {}
    for lab in LABELS:
        rule_ids = reasons.get(lab, []) or []
        labels[lab] = {
            "score": float(scores.get(lab, 0.0) or 0.0),
            "rules": [
                {"id": rid, "why": RULE_DESCRIPTIONS.get(rid, rid)}
                for rid in rule_ids
            ],
        }
    return _cached_json({"object": "explain", "labels": labels})


_PRINTINGS_SQL = text(
    "WITH c AS (SELECT oracle_id FROM cards WHERE id = :id), "
    "p AS (SELECT p.* FROM printings p JOIN c USING (oracle_id)), "
    "owned AS ("
    "  SELECT COALESCE(i.scryfall_id, solo.id) AS printing_id, "
    "         SUM(i.quantity) AS quantity, SUM(i.foil_quantity) AS foil_quantity "
    "  FROM inventory i JOIN c USING (oracle_id) "
    # Pre-Scryfall-ID rows: credit the set only when it holds one printing.
    "  LEFT JOIN LATERAL ("
    "    SELECT (array_agg(p.id))[1] AS id FROM p WHERE p.set_code = i.set_code "
    "    HAVING count(*) = 1"
    "  ) solo ON i.scryfall_id IS NULL "
    "  GROUP BY 1"
    ") "
    "SELECT c.oracle_id, p.id, p.set_code, p.set_name, p.collector_number, "
    "       p.released_at, p.rarity, p.finishes, p.image_small, p.image_normal, "
    "       p.usd, p.usd_foil, owned.quantity, owned.foil_quantity "
    "FROM c LEFT JOIN p ON true "
    "LEFT JOIN owned ON owned.printing_id = p.id "
    "ORDER BY owned.quantity IS NULL, p.released_at DESC NULLS LAST, p.collector_number"
)


def _price(v: Any) -> str | None:
    return None if v is None else str(v)


@router.get(
    "/{card_id}/printings",
    summary="Every printing of a card, with owned counts",
    description=(
        "All printings sharing the card's oracle, owned first then newest. "
        "`owned` is `{quantity, foil_quantity}` from inventory, matched by "
        "Scryfall ID, or null. Empty until the first refresh after upgrade. "
        "404 when the card is unknown."
    ),
    responses={404: _ERROR_RESPONSE, 422: _ERROR_RESPONSE},
)
async def get_card_printings(
    card_id: UUID,
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    async with engine.connect() as conn:  # type: ignore[attr-defined]
        rows = (await conn.execute(_PRINTINGS_SQL, {"id": str(card_id)})).all()
    if not rows:
        return _not_found(f"No card found with id '{card_id}'")
    data = [
        {
            "id": str(r.id),
            "set": r.set_code,
            "set_name": r.set_name,
            "collector_number": r.collector_number,
            "released_at": None if r.released_at is None else str(r.released_at),
            "rarity": r.rarity,
            "finishes": r.finishes,
            "image_uris": {"small": r.image_small, "normal": r.image_normal},
            "prices": {"usd": _price(r.usd), "usd_foil": _price(r.usd_foil)},
            "owned": None if r.quantity is None
            else {"quantity": int(r.quantity), "foil_quantity": int(r.foil_quantity)},
        }
        for r in rows
        if r.id is not None
    ]
    return _cached_json({"object": "list", "data": data})


@router.get(
    "/{card_id}/related",
    summary="Related cards (tokens, meld halves, combo pieces) — D14.8",
    description=(
        "Resolves every entry in `cards.all_parts` (Scryfall's per-printing "
        "related-card list) against our name index and hands back canonical "
        "card ids for the collapsed printings we serve. Each entry carries "
        "`component` (`token` / `meld_part` / `meld_result` / `combo_piece`), "
        "`name`, `type_line`, and the resolved card object under `card` "
        "(null when the referenced name isn't in our corpus — e.g. an "
        "unimplemented token type). 404 when the base card is unknown."
    ),
    responses={404: _ERROR_RESPONSE, 422: _ERROR_RESPONSE},
)
async def get_card_related(
    card_id: UUID,
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """List related cards for ``card_id`` (D14.8).

    Single statement: LEFT JOIN LATERAL over ``jsonb_array_elements`` on
    the JSONB ``all_parts`` column, then LEFT JOIN LATERAL again on
    ``cards`` by name (case-insensitive) so the response carries our
    canonical row (not the Scryfall printing id in ``all_parts[*].id``,
    which may point at a printing we didn't keep).

    Empty ``data`` (200) when the card exists but has no related parts;
    404 when the card itself is unknown so the UI can distinguish "no
    related" from "stale id".
    """
    async with engine.connect() as conn:  # type: ignore[attr-defined]
        result = await conn.execute(
            text(
                "SELECT c.oracle_id AS root_oracle_id, "
                "       part_ord.part->>'component' AS component, "
                "       part_ord.part->>'name'      AS name, "
                "       part_ord.part->>'type_line' AS type_line, "
                "       part_ord.part->>'id'        AS scryfall_id, "
                "       resolved.id  AS resolved_id, "
                "       resolved.raw AS resolved_raw "
                "FROM cards c "
                "LEFT JOIN LATERAL jsonb_array_elements("
                "    COALESCE(c.all_parts, '[]'::jsonb)"
                "  ) WITH ORDINALITY part_ord(part, ord) ON true "
                "LEFT JOIN LATERAL ("
                "  SELECT rc.id, rc.raw FROM cards rc "
                "  WHERE LOWER(rc.name) = LOWER(part_ord.part->>'name') "
                "  LIMIT 1"
                ") resolved ON true "
                "WHERE c.id = :id "
                "ORDER BY part_ord.ord NULLS FIRST"
            ),
            {"id": str(card_id)},
        )
        rows = result.all()
    if not rows:
        return _not_found(f"No card found with id '{card_id}'")
    data = [
        {
            "component": r.component,
            "name": r.name,
            "type_line": r.type_line,
            "card": r.resolved_raw,
        }
        for r in rows
        if r.component is not None
    ]
    return _cached_json({"object": "list", "data": data})


@router.get(
    "/random",
    summary="Random card (optionally satisfying a DSL filter)",
    description=(
        "Returns one random card from `cards`. When `q` is supplied, the "
        "DSL filter narrows the pool via the same parser as `/cards/search`. "
        "Not cached — random by design. 404 when `q` matches nothing. Spec "
        "§1.2 originally listed this as a non-goal; D17.3 promoted it for "
        "MCP agents that ask for 'surprise me' / flavor-exploration prompts."
    ),
    responses={404: _ERROR_RESPONSE, 422: _ERROR_RESPONSE},
)
async def random_card(
    q: str | None = Query(
        default=None,
        description=(
            "Optional DSL filter per §5. Empty / whitespace is treated as "
            "unfiltered (unlike `/cards/search` where empty `q` is a 422)."
        ),
        examples=["t:planeswalker", "c:g cmc<=3 f:commander"],
    ),
    fields: str | None = Query(
        default=None,
        description=(
            "Comma-separated dotted-path projection per §7. Shorter alias "
            "for `?projection=` — both names accept identical CSV syntax; "
            "when both are supplied, `fields` wins (D15.29)."
        ),
        examples=["name,type_line"],
    ),
    projection: str | None = Query(
        default=None,
        include_in_schema=False,
    ),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Pick one random row from `cards`, optionally filtered by DSL (§6.12).

    The unfiltered branch fires `ORDER BY random() LIMIT 1` on the whole
    corpus; on ~30k rows Postgres handles this in <100 ms without special
    handling (`TABLESAMPLE SYSTEM` would help at 10× scale — deferred).

    Errors:
      * `q` > `SEARCH_Q_MAX_LEN` → 422 `bad_request` (mirrors `/search`).
      * DSL parse fails → 422 `bad_request`.
      * §5.2 rejected key → 422 `unsupported_operator`.
      * `q` narrows to zero rows → 404 `not_found`.
    """
    # D15.29 — `?fields=` alias wins when both supplied.
    projection = fields if fields is not None else projection
    where: sa.ColumnElement[bool] = sa.true()
    trimmed = q.strip() if q else ""
    if trimmed:
        if len(q or "") > SEARCH_Q_MAX_LEN:
            return _scryfall_error(422, "bad_request", "q too long")
        try:
            ast = parse(trimmed)
            where = compile_ast(ast, _CARDS)
        except UnsupportedOperatorError as exc:
            return _scryfall_error(422, "unsupported_operator", str(exc))
        except (ParseError, ValueError) as exc:
            return _scryfall_error(422, "bad_request", str(exc))

    stmt = (
        sa.select(
            _CARDS.c.raw.label("raw"),
            _CARDS.c.predicted_labels.label("predicted_labels"),
            _CARDS.c.label_scores.label("label_scores"),
        )
        .where(where)
        .order_by(sa.func.random())
        .limit(1)
    )
    async with engine.connect() as conn:  # type: ignore[attr-defined]
        result = await conn.execute(stmt)
        rows = result.mappings().all()

    if not rows:
        details = f"No cards matched '{trimmed}'" if trimmed else "cards table is empty"
        return _not_found(details)
    row = rows[0]
    payload = _decorate(row["raw"], row["predicted_labels"], row["label_scores"])
    projected = _apply_projection(payload, projection)
    return projected


@router.get(
    "/{card_id}",
    summary="Single card lookup by Scryfall UUID",
    description=(
        "Returns the original Scryfall payload verbatim (from `cards.raw`). "
        "404 when no row matches; 422 when `:card_id` is not a valid UUID "
        "or `?projection=` is malformed."
    ),
    responses={404: _ERROR_RESPONSE, 422: _ERROR_RESPONSE},
)
async def get_card_by_id(
    card_id: UUID,
    request: Request,
    fields: str | None = Query(
        default=None,
        description=(
            "Comma-separated dotted-path projection per §7. Shorter alias "
            "for `?projection=` — both names accept identical CSV syntax; "
            "when both are supplied, `fields` wins (D15.29)."
        ),
        examples=["name,oracle_text"],
    ),
    projection: str | None = Query(
        default=None,
        include_in_schema=False,
    ),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Look up a single card by Scryfall UUID per §6.2.

    The ``raw`` JSONB column stores the original Scryfall payload; we
    return it verbatim (or projected through ``?projection=`` per §7 when
    supplied) so the wire body matches what an agent would receive from
    Scryfall directly.

    Returns 404 with a Scryfall-shaped error body when no row matches.
    Invalid UUIDs are rejected by FastAPI's path-param validator (422).
    Invalid ``projection=`` → 422 with ``invalid_projection`` (D7.4).
    """
    # D15.29 — `?fields=` alias wins when both supplied.
    projection = fields if fields is not None else projection

    async with engine.connect() as conn:  # type: ignore[attr-defined]
        result = await conn.execute(
            text(
                "SELECT oracle_id, raw, predicted_labels, label_scores, "
                "label_reasons, is_commander FROM cards WHERE id = :id"
            ),
            {"id": str(card_id)},
        )
        row = result.one_or_none()

    if row is None:
        return _not_found(f"No card found with id '{card_id}'")
    payload = _decorate(
        row.raw, row.predicted_labels, row.label_scores, row.label_reasons
    )
    payload["is_commander"] = bool(row.is_commander)
    projected = _apply_projection(payload, projection)
    if isinstance(projected, JSONResponse):
        return projected
    return _cached_json(projected)


# Manual label override — a human correction for a miscategorised card.
# The rules-layer classifier (classify/rules.py) is deliberately strict and
# has known gaps (cost-reduction-as-ramp, card-selection-as-CA); rather than
# grow regexes that re-litigate what each slot means, a correction is pinned
# per card here. It writes `predicted_labels` directly so every consumer
# (analyze, slot counts, DSL `pred:`) picks it up, and records the intent
# under the reserved `_override` key of `label_reasons` so a later
# re-classify carries it forward instead of wiping it (see refresh.py).


class _SetLabelsRequest(BaseModel):
    """Body for ``PUT /cards/{id}/labels`` — the exact label set to pin."""

    labels: list[str] = Field(
        default_factory=list,
        description=(
            "The full slot-label set to force for this card (replaces the "
            f"rules verdict). Each must be one of: {', '.join(LABELS)}. "
            "An empty list pins the card to no slot."
        ),
    )


def _rules_verdict(
    oracle_text: Any, card_faces: Any
) -> tuple[list[str], dict[str, float], dict[str, list[str]]]:
    """Fresh rules-layer verdict for one card — labels, scores, reasons."""
    scores, reasons = classify_by_rules_ex(
        {"oracle_text": oracle_text, "card_faces": card_faces}
    )
    labels = [lab for lab in LABELS if scores[lab] >= 0.5]
    return labels, scores, reasons


@router.put(
    "/{card_id}/labels",
    summary="Pin a manual slot-label override for one card",
    description=(
        "Replaces the rules-layer `predicted_labels` for this card with the "
        "supplied set and marks it a manual override that survives a "
        "re-classify. Body: `{\"labels\": [...]}` (subset of the five slot "
        "labels). 404 when no card matches; 422 on an unknown label."
    ),
    responses={404: _ERROR_RESPONSE, 422: _ERROR_RESPONSE},
)
async def set_card_labels(
    card_id: UUID,
    req: _SetLabelsRequest,
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Pin ``req.labels`` as this card's labels (see the override note above)."""
    unknown = [lab for lab in req.labels if lab not in LABELS]
    if unknown:
        return _scryfall_error(
            422,
            "invalid_labels",
            f"Unknown label(s): {', '.join(unknown)}. Allowed: {', '.join(LABELS)}.",
        )
    # Canonical order + dedup, so the stored set matches the rules-layer order.
    labels = [lab for lab in LABELS if lab in set(req.labels)]
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "UPDATE cards SET predicted_labels = :labels, "
                    "label_reasons = jsonb_set("
                    "  coalesce(label_reasons, '{}'::jsonb), '{_override}', :ov, true) "
                    "WHERE id = :id "
                    "RETURNING raw, predicted_labels, label_scores, label_reasons"
                ),
                {"labels": labels, "ov": Jsonb(labels), "id": str(card_id)},
            )
        ).one_or_none()
    if row is None:
        return _not_found(f"No card found with id '{card_id}'")
    return _decorate(
        row.raw, row.predicted_labels, row.label_scores, row.label_reasons
    )


@router.delete(
    "/{card_id}/labels",
    summary="Clear a card's manual override, reverting to the rules verdict",
    description=(
        "Removes any manual label override and recomputes `predicted_labels` "
        "from the rules-layer classifier. 404 when no card matches."
    ),
    responses={404: _ERROR_RESPONSE, 422: _ERROR_RESPONSE},
)
async def clear_card_labels(
    card_id: UUID,
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Revert this card to the rules-layer verdict, dropping any override."""
    async with engine.begin() as conn:
        src = (
            await conn.execute(
                text("SELECT oracle_text, card_faces FROM cards WHERE id = :id"),
                {"id": str(card_id)},
            )
        ).one_or_none()
        if src is None:
            return _not_found(f"No card found with id '{card_id}'")
        labels, scores, reasons = _rules_verdict(src.oracle_text, src.card_faces)
        row = (
            await conn.execute(
                text(
                    "UPDATE cards SET predicted_labels = :labels, "
                    "label_scores = :scores, label_reasons = :reasons "
                    "WHERE id = :id "
                    "RETURNING raw, predicted_labels, label_scores, label_reasons"
                ),
                {
                    "labels": labels,
                    "scores": Jsonb(scores),
                    "reasons": Jsonb(reasons),
                    "id": str(card_id),
                },
            )
        ).one()
    return _decorate(
        row.raw, row.predicted_labels, row.label_scores, row.label_reasons
    )


# D17.5 — POST /cards/collection Scryfall-parity bulk lookup.


class _CollectionIdentifier(BaseModel):
    """One entry in the D17.5 ``identifiers`` list.

    All fields optional. At least one of ``id`` / ``oracle_id`` / ``name``
    must be present — otherwise the entry is undecidable and surfaces
    under ``not_found`` unchanged.
    """

    id: str | None = None
    oracle_id: str | None = None
    name: str | None = None


class _CollectionRequest(BaseModel):
    """Body for ``POST /cards/collection`` (§6.26)."""

    identifiers: list[_CollectionIdentifier] = Field(
        default_factory=list,
        max_length=75,
        description=(
            "Up to 75 identifiers per Scryfall's own cap. Each entry may "
            "specify `id`, `oracle_id`, or `name` (case-insensitive)."
        ),
    )


_COLLECTION_SQL = text(
    "SELECT id, oracle_id, name, raw, predicted_labels, label_scores "
    "FROM cards "
    "WHERE id::text = ANY(CAST(:ids AS text[])) "
    "   OR oracle_id::text = ANY(CAST(:oids AS text[])) "
    "   OR LOWER(name) = ANY(:names)"
)


@router.post(
    "/collection",
    summary="Bulk card lookup by id / oracle_id / name (§6.26)",
    description=(
        "Scryfall-parity bulk lookup — one round-trip resolves up to 75 "
        "cards keyed by any mix of `id` / `oracle_id` / `name` (case-"
        "insensitive). Response `data` preserves the request order; "
        "identifiers with no match surface unchanged under `not_found`. "
        "Kills the N-request pattern of agents rebuilding decklists from "
        "stored ids."
    ),
    responses={422: _ERROR_RESPONSE},
)
async def lookup_collection(
    body: _CollectionRequest = Body(
        ...,
        examples=[
            {
                "identifiers": [
                    {"id": "0000ec70-1c4e-4ad5-8d77-d3963fa1fa42"},
                    {"name": "Sol Ring"},
                    {"oracle_id": "aaaa1111-1111-1111-1111-111111111111"},
                ]
            }
        ],
    ),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """One SQL over three OR'd predicates; Python-side match-back."""
    identifiers = body.identifiers
    if not identifiers:
        return {"object": "list", "data": [], "not_found": []}

    ids = [i.id for i in identifiers if i.id]
    oids = [i.oracle_id for i in identifiers if i.oracle_id]
    names = [i.name.lower() for i in identifiers if i.name]

    async with engine.connect() as conn:  # type: ignore[attr-defined]
        result = await conn.execute(
            _COLLECTION_SQL,
            {"ids": ids, "oids": oids, "names": names},
        )
        rows = list(result.all())

    by_id: dict[str, Any] = {str(r.id): r for r in rows}
    by_oracle: dict[str, Any] = {str(r.oracle_id): r for r in rows}
    by_name: dict[str, Any] = {r.name.lower(): r for r in rows}

    data: list[Any] = []
    not_found: list[dict[str, str]] = []
    for ident in identifiers:
        hit = None
        if ident.id and ident.id in by_id:
            hit = by_id[ident.id]
        elif ident.oracle_id and ident.oracle_id in by_oracle:
            hit = by_oracle[ident.oracle_id]
        elif ident.name and ident.name.lower() in by_name:
            hit = by_name[ident.name.lower()]

        if hit is None:
            # Preserve the exact shape the caller sent — drop unset fields.
            not_found.append(
                {k: v for k, v in ident.model_dump().items() if v is not None}
            )
            continue

        data.append(
            _decorate(hit.raw, hit.predicted_labels, hit.label_scores)
        )

    return {"object": "list", "data": data, "not_found": not_found}


# POST /cards/tag-counts — per-tag counts across a set of cards (by name).


class _TagCountsRequest(BaseModel):
    """Body for ``POST /cards/tag-counts``."""

    names: list[str] = Field(
        default_factory=list,
        max_length=1000,
        description="Card names (case-insensitive) to aggregate oracle tags over.",
    )


_TAG_COUNTS_SQL = text(
    "WITH oid AS ("
    "  SELECT DISTINCT oracle_id FROM cards WHERE LOWER(name) = ANY(:names)"
    ") "
    "SELECT ot.tag AS tag, COUNT(DISTINCT ot.oracle_id) AS n "
    "FROM oracle_tags ot JOIN oid ON oid.oracle_id = ot.oracle_id "
    "GROUP BY ot.tag"
)


@router.post(
    "/tag-counts",
    summary="Per-tag card counts across a set of cards",
    description=(
        "Given a list of card names, returns how many of them carry each "
        "oracle tag — `{tag: count}` — in one aggregate query. Tag "
        "hierarchy is flattened (matches `otag:` / `/cards/:id/tags`), and "
        "counts are of distinct cards, so reprints collapse. Powers the "
        "deck-builder's theme chips (how many deck cards serve each theme)."
    ),
    responses={422: _ERROR_RESPONSE},
)
async def tag_counts(
    body: _TagCountsRequest = Body(
        ..., examples=[{"names": ["Krenko, Mob Boss", "Goblin Matron"]}]
    ),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Aggregate oracle-tag counts across the named cards (one query)."""
    names = [n.lower() for n in body.names if n and n.strip()]
    if not names:
        return {"object": "tag_counts", "counts": {}}
    async with engine.connect() as conn:  # type: ignore[attr-defined]
        result = await conn.execute(_TAG_COUNTS_SQL, {"names": names})
        rows = result.all()
    return {"object": "tag_counts", "counts": {r[0]: r[1] for r in rows}}


# POST /cards/resolve-pt — Portuguese printed-name → oracle_id resolver.


class _ResolvePtRequest(BaseModel):
    """Body for ``POST /cards/resolve-pt``."""

    names: list[str] = Field(
        default_factory=list,
        max_length=1000,
        description=(
            "Card names as they appear on a pt-BR source (LigaMagic deck "
            "pages). Portuguese printed names resolve against `pt_names`; "
            "names of cards never printed in Portuguese fall back to an "
            "exact match on the English `cards.name`."
        ),
    )


@router.post(
    "/resolve-pt",
    summary="Resolve Portuguese card names to oracle ids",
    description=(
        "Batch-resolves Portuguese printed names (with a pg_trgm fuzzy "
        "fallback) plus an English-exact fallback for cards never printed "
        "in Portuguese. Returns `matches` (input name → `oracle_id`) and "
        "`unresolved` (inputs that missed every pass). Requires the "
        "`pt_names` table to be populated by `scripts/build_pt_names.py`; "
        "an empty table simply yields everything under `unresolved`."
    ),
    responses={422: _ERROR_RESPONSE},
)
async def resolve_pt(
    body: _ResolvePtRequest = Body(
        ...,
        examples=[{"names": ["Banco de Nevoeiro", "Anel Solar"]}],
    ),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Thin HTTP wrapper over :func:`resolve_pt_names`."""
    if not body.names:
        return {"object": "resolve_pt", "matches": {}, "unresolved": []}

    async with engine.connect() as conn:  # type: ignore[attr-defined]
        result = await resolve_pt_names(conn, body.names)

    return {
        "object": "resolve_pt",
        "matches": {name: str(oid) for name, oid in result.matches.items()},
        "unresolved": result.unresolved,
    }


# D17.6 — POST /cards/search/validate DSL preflight.


class _ValidateRequest(BaseModel):
    """Body for ``POST /cards/search/validate`` (§6.27)."""

    q: str = Field(..., description="DSL query to validate.")


def _did_you_mean(offending_key: str) -> list[str]:
    """Difflib-close matches over ALL_KNOWN_KEYS.

    Cutoff 0.6 matches Scryfall's own typo tolerance; n=3 keeps the
    hint short. Returns [] when nothing lands above the cutoff.
    """
    from difflib import get_close_matches

    from biblioplex.dsl.compiler import ALL_KNOWN_KEYS

    return get_close_matches(offending_key, ALL_KNOWN_KEYS, n=3, cutoff=0.6)


def _extract_offending_key(msg: str) -> str | None:
    """Pull the key name out of a stringified `UnsupportedOperatorError`.

    Compiler raises `"unsupported operator: <key>:"` or
    `"unknown predicted label: <label>"`. Take the last whitespace-
    separated token, strip trailing punctuation.
    """
    if not msg:
        return None
    tail = msg.strip().rstrip(":=<>! ").split()[-1]
    return tail.rstrip(":=<>! ") or None


@router.post(
    "/search/validate",
    summary="DSL preflight — parse `q`, report ok/error + did_you_mean",
    description=(
        "Compiles `q` through the same parser + compiler pipeline as "
        "`/cards/search` without touching the DB. Success → 200 with "
        "`ast_summary` + the deployment's `SEARCH_DEFAULT_FILTER` (so "
        "agents can see the shadow filter without diffing). Failure → "
        "422 with `did_you_mean` populated when the failure is a typo-"
        "correctable key. Kills the DSL-tune-by-retrying loop."
    ),
    responses={422: _ERROR_RESPONSE},
)
async def validate_search(
    body: _ValidateRequest = Body(
        ...,
        examples=[{"q": "t:instant c:r f:commander"}],
    ),
    request: Request = None,  # type: ignore[assignment]
) -> Any:
    """Preflight only; no DB touch."""
    q = body.q.strip()
    if not q:
        return _scryfall_error(422, "bad_request", "q is required")

    default_filter: str | None = getattr(
        request.app.state, "search_default_filter", None
    ) or None

    try:
        ast = parse(q)
        compile_ast(ast, _CARDS)
    except UnsupportedOperatorError as exc:
        key = _extract_offending_key(str(exc))
        hint = _did_you_mean(key) if key else []
        return JSONResponse(
            status_code=422,
            content={
                "object": "error",
                "status": 422,
                "code": "unsupported_operator",
                "message": "unsupported operator in q",
                "details": str(exc),
                "did_you_mean": hint or [],
            },
        )
    except (ParseError, ValueError) as exc:
        return JSONResponse(
            status_code=422,
            content={
                "object": "error",
                "status": 422,
                "code": "bad_request",
                "message": "failed to parse q",
                "details": str(exc),
                "did_you_mean": None,
            },
        )

    return {
        "ok": True,
        "ast_summary": repr(ast),
        "applied_default_filter": default_filter,
    }


# Re-export HTTPException so future deliverables in this module can raise
# it without re-importing; keeps the public surface of `routers.cards`
# explicit when D2.2+ land.
__all__ = ["router", "get_read_engine", "HTTPException"]
