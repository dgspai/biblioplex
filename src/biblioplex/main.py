"""FastAPI application entrypoint.

Wires the settings (`§10`) and engines (`§9`) into a single :class:`FastAPI`
instance. Endpoints attach in later milestones; M0 ships the app shell plus
``GET /health`` (D0.4).
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from fastapi_mcp import FastApiMCP
from mcp.types import ToolAnnotations
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from biblioplex import __version__
from biblioplex.config import Settings, get_settings
from biblioplex.db import make_read_engine, make_refresh_engine
from biblioplex.embed.model import assert_model_dim, load_model
from biblioplex.ingest.refresh import ADVISORY_LOCK_KEY, AlreadyRunning, RefreshStatus
from biblioplex.routers.admin import build_refresh_runner
from biblioplex.routers.admin import router as admin_router
from biblioplex.routers.cards import router as cards_router
from biblioplex.routers.catalog import router as catalog_router
from biblioplex.routers.combos import router as combos_router
from biblioplex.routers.commanders import router as commanders_router
from biblioplex.routers.deck_library import router as deck_library_router
from biblioplex.routers.decks import router as decks_router
from biblioplex.routers.health import router as health_router
from biblioplex.routers.inventory import router as inventory_router
from biblioplex.routers.uploads import router as uploads_router
from biblioplex.routers.wishlist import router as wishlist_router
from biblioplex.scryfall import catalogs as scryfall_catalogs

_log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Boot the embedding model + dim-check (§8.1); dispose engines on shutdown.

    Loading the model at startup makes the first request's latency
    predictable (no first-call HF download stalling /cards/:id/similar)
    and lets :func:`assert_model_dim` fail-fast on a mismatched
    ``HF_EMBED_DIM`` before any request can hit the DB.
    """
    settings: Settings = app.state.settings
    model = load_model(settings)
    assert_model_dim(model, settings)
    app.state.embed_model = model
    # Snapshot Scryfall keyword catalogs once per process so the DSL
    # compiler can resolve `is:frenchvanilla` against the canonical
    # whitelist instead of the §5.1 punctuation heuristic. Best-effort:
    # any failure leaves the cache empty and search keeps working via
    # the legacy fallback in `dsl/compiler.py`.
    try:
        async with httpx.AsyncClient() as client:
            kws = await scryfall_catalogs.fetch_keyword_set(client)
        scryfall_catalogs.set_keyword_set(kws)
        _log.info("scryfall keyword catalog loaded (%d entries)", len(kws))
    except Exception as exc:  # noqa: BLE001 — best-effort cache warm
        _log.warning(
            "scryfall keyword catalog fetch failed; using fallback heuristic: %s",
            exc,
        )
    if settings.auto_refresh_if_empty:
        await _auto_refresh_if_empty(app)
    try:
        yield
    finally:
        # Cancel + await the auto-refresh task (if any) BEFORE engine
        # disposal so a half-finished ingest can't race the pool teardown.
        # `CancelledError` is the expected exit signal — suppress it; any
        # other exception has already been logged inside `_run`.
        task = getattr(app.state, "_auto_refresh_task", None)
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        read_engine: AsyncEngine = app.state.read_engine
        refresh_engine: AsyncEngine = app.state.refresh_engine
        await read_engine.dispose()
        await refresh_engine.dispose()


async def _auto_refresh_if_empty(app: FastAPI) -> None:
    """Kick `/admin/refresh` in the background when `cards` is empty
    or no card has an embedding (interrupted first ingest).

    Best-effort: any DB error (table missing, server unreachable) is
    logged and swallowed so a misconfigured DB still surfaces via the
    normal `/health` path instead of crashing boot.

    The created `asyncio.Task` is stored on `app.state._auto_refresh_task`
    so the event loop's weak task-set can't GC it mid-flight (Python's
    `asyncio` docs warn that fire-and-forget tasks may be collected); the
    `lifespan` shutdown branch cancels + awaits it before disposing
    engines.
    """
    read_engine: AsyncEngine = app.state.read_engine
    try:
        # F1: engine.begin() opens the txn up front; execute() on a plain
        # connect() autobegins and a later conn.begin() would raise.
        # Bug.7: probe the advisory lock BEFORE minting a job so N
        # workers booting in parallel don't each fan out a runner
        # that would lose on the same lock inside `run_locked`.
        # Bug.9: use the txn-scoped variant — auto-released when
        # the txn ends (commit or rollback), so a raise between acquire
        # and unlock can't leak the lock onto a pooled connection. Small
        # race remains (another worker may win between our txn end and
        # the runner's acquire), but the runner's finally-block + Bug.6
        # gate mean the loser neither bumps epoch nor wastes stage
        # bookkeeping.
        async with read_engine.begin() as conn:
            result = await conn.execute(text("SELECT 1 FROM cards LIMIT 1"))
            if result.first() is not None:
                # An interrupted first ingest leaves cards but no embeddings
                # (embed runs right after ingest). "None at all" is the
                # signal; a few un-embedded cards on a healthy DB is not.
                embedded = await conn.execute(
                    text("SELECT 1 FROM cards WHERE embedding IS NOT NULL LIMIT 1")
                )
                if embedded.first() is not None:
                    return
                _log.info("auto-refresh: cards without embeddings; resuming refresh")
            granted_row = await conn.execute(
                text(f"SELECT pg_try_advisory_xact_lock({ADVISORY_LOCK_KEY})")
            )
            if not bool(granted_row.scalar_one()):
                _log.info("auto-refresh: advisory lock held elsewhere; skipping")
                return
    except Exception as exc:  # noqa: BLE001 — best-effort startup probe
        _log.warning("auto-refresh probe failed; skipping: %s", exc)
        return

    status: RefreshStatus = app.state.refresh_status
    job_id = str(uuid.uuid4())
    if status.try_mark_started(job_id) is not None:
        return  # already running (e.g. multi-worker race)

    runner = build_refresh_runner(app)

    async def _run() -> None:
        try:
            await runner.run_locked()
        except AlreadyRunning:
            return
        except Exception:
            _log.exception("auto-refresh failed")
        finally:
            # Drop the strong ref now that the coroutine has finished, so
            # repeated lifespan boots in a long-running test process don't
            # pile up completed-task references.
            if getattr(app.state, "_auto_refresh_task", None) is asyncio.current_task():
                app.state._auto_refresh_task = None

    _log.info("cards table empty; kicking auto-refresh job_id=%s", job_id)
    app.state._auto_refresh_task = asyncio.create_task(_run())


API_DESCRIPTION = (
    "Scryfall-compatible MTG card API with vector similarity. "
    "Implements a subset of `api.scryfall.com` "
    "(`/cards/search`, `/cards/:id`, `/cards/named`) plus `/cards/:id/similar` "
    "for embedding-based nearest-neighbour lookup. Designed for AI-agent "
    "consumption: wire shape mirrors Scryfall so the same clients work "
    "against either backend. See `docs/design/tech_spec.md` for the full surface."
)


OPENAPI_TAGS: list[dict[str, str]] = [
    {"name": "cards", "description": "Card lookup, DSL search, and similarity."},
    {"name": "combos", "description": "Commander Spellbook combo browse/search."},
    {"name": "commanders", "description": "Commander discovery by tags + colors (D17.2)."},
    {"name": "catalog", "description": "Slug + set catalog discovery for MCP agents (D17.4)."},
    {
        "name": "decks",
        "description": (
            "Decklist analysis vs template slot targets (§6.7), plus the "
            "stored-deck library (D19.4)."
        ),
    },
    {"name": "inventory", "description": "Card collection inventory and Deckbox sync."},
    {"name": "wishlist", "description": "Wanted-card list from a Deckbox wishlist export."},
    {"name": "uploads", "description": "History of imported CSV files (D18.17)."},
    {"name": "admin", "description": "Bulk re-ingest pipeline."},
    {"name": "health", "description": "Liveness + readiness probe."},
]


# D17.1 — MCP tool names read the route function name directly instead of
# FastAPI's auto-generated `<func>_<path>_<method>` mash. See
# `process.md` §10.1 for the naming discipline this enforces.
def _operation_id_from_route_name(route: APIRoute) -> str:
    return route.name


# D17.1 — endpoints that stay REST-only (never surface as MCP tools).
# `trigger_refresh` fires a ~1h Scryfall bulk download + embed pass;
# agents must not be able to kick it off. `check_liveness` (D16.20) is
# a k8s liveness probe — no signal for agents.
MCP_EXCLUDED_OPERATIONS: list[str] = ["trigger_refresh", "check_liveness"]

# POST tools that only compute over the request body and store nothing. Every
# other POST/PUT/PATCH is a write. A new compute-only POST missing here is
# reported as a write (the safe direction).
MCP_READONLY_POSTS: frozenset[str] = frozenset({
    "lookup_collection",
    "tag_counts",
    "resolve_pt",
    "validate_search",
    "analyze_decklist",
    "compare_decklists",
    "diff_decklists",
    "export_decklist",
    "price_decklist",
    "suggest_decklist",
    "sideboard_suggest_decklist",
    "decklist_combos",
    "decklist_near_combos",
    "synergy_decklist",
    "decklist_mulligan",
    "goldfish_decklist",
    "decklist_bracket",
    "decklist_upgrade",
    "decklist_manabase",
    "decklist_legality",
})


def _annotate_mcp_tools(mcp: FastApiMCP) -> None:
    """Set MCP tool annotations from each tool's HTTP method.

    fastapi-mcp emits none. `mcp.tools` / `mcp.operation_map` are its public
    attributes and `list_tools` returns `mcp.tools` lazily, so annotating after
    construction (before mount) reaches clients. ponytail: derived from the
    HTTP method + MCP_READONLY_POSTS; per-route overrides if that gets too coarse.
    """
    for tool in mcp.tools:
        method = mcp.operation_map[tool.name]["method"].lower()
        if method == "get" or tool.name in MCP_READONLY_POSTS:
            tool.annotations = ToolAnnotations(readOnlyHint=True)
        elif method == "delete":
            tool.annotations = ToolAnnotations(readOnlyHint=False, destructiveHint=True)
        else:
            tool.annotations = ToolAnnotations(readOnlyHint=False)


# D16.16 — MCP bearer-token gate. Applied via middleware so mount_http()
# stays declarative and every /mcp* path inherits the check. Empty
# token leaves the transport open (dev default). Constant-time compare
# via hmac.compare_digest — timing-attack surface exists even on a
# shared secret.
def _install_mcp_auth_middleware(app: FastAPI, token: str) -> None:
    if not token:
        return
    expected = token.encode()

    @app.middleware("http")
    async def _mcp_auth_gate(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        if not (path == "/mcp" or path.startswith("/mcp/")):
            return await call_next(request)
        header = request.headers.get("Authorization", "")
        scheme, _, presented = header.partition(" ")
        if scheme != "Bearer" or not hmac.compare_digest(
            presented.encode(), expected
        ):
            return JSONResponse(
                status_code=401,
                content={
                    "object": "error",
                    "status": 401,
                    "code": "unauthorized",
                    "details": "invalid or missing bearer token",
                },
            )
        return await call_next(request)


# CSRF guard. Mutating routes have no auth, so a hostile page in the user's
# browser could otherwise POST to localhost. Browsers always send `Origin` on
# cross-site unsafe requests; curl / MCP clients / server-to-server send none
# and pass. Allowed: same host:port as the request or a CORS_ORIGINS entry.
# Not auth: any non-browser client can omit or forge Origin.
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _install_csrf_guard(app: FastAPI, cors_origins: list[str]) -> None:
    allowed = set(cors_origins)

    @app.middleware("http")
    async def _csrf_origin_gate(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        origin = request.headers.get("Origin")
        if (
            origin is None
            or request.method not in _UNSAFE_METHODS
            or "*" in allowed
            or origin in allowed
            or origin.partition("://")[2] == request.headers.get("Host", "")
        ):
            return await call_next(request)
        return JSONResponse(
            status_code=403,
            content={
                "object": "error",
                "status": 403,
                "code": "forbidden_origin",
                "details": f"cross-site write from origin {origin!r} blocked; "
                "add it to CORS_ORIGINS to allow",
            },
        )


async def _validation_error_envelope(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    """422 in the Scryfall envelope; per-field messages go in `warnings`."""
    warnings = [
        f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "object": "error",
            "status": 422,
            "code": "bad_request",
            "details": "; ".join(warnings) or "invalid request",
            "warnings": warnings,
        },
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Construct the FastAPI application.

    Settings are read from the environment unless explicitly injected (tests).
    """
    resolved = settings or get_settings()
    app = FastAPI(
        title="Biblioplex",
        version=__version__,
        description=API_DESCRIPTION,
        openapi_tags=OPENAPI_TAGS,
        lifespan=lifespan,
        generate_unique_id_function=_operation_id_from_route_name,
    )
    app.add_exception_handler(RequestValidationError, _validation_error_envelope)  # type: ignore[arg-type]
    app.state.settings = resolved
    # D16.6: strip the deployment-wide search filter once at boot. The /search
    # hot path reads it on every request; per-request `.strip()` is cheap but
    # the cached value keeps the hot path allocation-free.
    app.state.search_default_filter = resolved.search_default_filter.strip()
    # gzip added before CORS so CORS becomes outermost (last added = outermost
    # in Starlette). CORS preflight bypasses compression; large search payloads
    # get compressed. 1000-byte floor skips single-card GETs.
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    # CORS per §10. CSV-split the env var; `*` short-circuits to the wildcard
    # list. Empty (default) installs no CORS middleware: the UI is same-origin
    # via nginx /api, so browsers get no cross-origin read access.
    origins = [o.strip() for o in resolved.cors_origins.split(",") if o.strip()]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    _install_csrf_guard(app, origins)
    # Construct engines eagerly so dependencies that read app.state.read_engine
    # work outside the lifespan context (e.g. TestClient / ASGITransport in
    # tests). Lifespan dispose remains in charge of teardown when invoked.
    app.state.read_engine = make_read_engine(resolved.database_url)
    app.state.refresh_engine = make_refresh_engine(resolved.database_url)
    # Process-local refresh telemetry — /health snapshots it, /admin/refresh
    # mutates it. Single instance per app so endpoints share the same view.
    app.state.refresh_status = RefreshStatus()
    app.include_router(health_router)
    app.include_router(admin_router)
    app.include_router(cards_router)
    app.include_router(combos_router)
    app.include_router(commanders_router)
    app.include_router(catalog_router)
    app.include_router(decks_router)
    # After decks_router: the stateless surface is all literal sub-paths, so
    # `/decks/analyze` keeps matching before `/decks/{deck_id}` is considered.
    app.include_router(deck_library_router)
    app.include_router(inventory_router)
    app.include_router(wishlist_router)
    app.include_router(uploads_router)
    _install_mcp_auth_middleware(app, resolved.mcp_auth_token)
    # D17.1 — mount the MCP transport last, once every route is
    # registered, so `FastApiMCP` picks up the full tool list. `/mcp`
    # (streamable HTTP) is the transport agents connect to:
    #   claude mcp add --transport http biblioplex http://localhost:8000/mcp
    mcp = FastApiMCP(
        app,
        name="Biblioplex",
        description=(
            "MTG card search (DSL), vector similarity, oracle tags, "
            "combos, and decklist analysis. Scryfall-shaped payloads."
        ),
        exclude_operations=MCP_EXCLUDED_OPERATIONS,
    )
    _annotate_mcp_tools(mcp)
    mcp.mount_http()
    return app


app: FastAPI = create_app()
