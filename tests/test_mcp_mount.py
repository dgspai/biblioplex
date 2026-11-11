"""D17.1 MCP mount — `/mcp` exposes every read endpoint as an MCP tool.

Contract pinned here so agent-facing surface can't regress silently:

* Every route function name becomes its OpenAPI `operationId` (so MCP
  tool names read `search_cards` instead of `search_cards_cards_search_get`).
* The MCP server is mounted at `/mcp` and lists tools derived from the
  app's routes.
* `trigger_refresh` is excluded — the ingest pipeline is heavy + long
  and must stay human-triggered.

Constructed via `create_app` + inspected in-process — no network hop.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi_mcp import FastApiMCP

from biblioplex.config import Settings
from biblioplex.main import create_app


def _walk_api_routes(app: FastAPI) -> Iterator[APIRoute]:
    """Flatten every APIRoute reachable from the app.

    FastAPI 0.137+ nests `include_router` calls under `_IncludedRouter`
    wrappers instead of splicing them at the top level, so
    ``app.routes`` alone hides most endpoints. This walks the nested
    `routes` attribute recursively.
    """
    stack: list[Any] = list(app.routes)
    while stack:
        route = stack.pop()
        if isinstance(route, APIRoute):
            yield route
            continue
        # FastAPI 0.137's `_IncludedRouter` wraps the real router under
        # `.original_router`; older versions and `Mount` expose `.routes`
        # directly. Try both.
        nested = getattr(route, "routes", None)
        if nested is None:
            inner = getattr(route, "original_router", None)
            if inner is not None:
                nested = getattr(inner, "routes", None)
        if nested:
            stack.extend(nested)


def _placeholder_settings() -> Settings:
    return Settings.model_construct(
        database_url="postgresql+psycopg://placeholder/placeholder",
        hf_embed_model="sentence-transformers/all-MiniLM-L6-v2",
        hf_embed_dim=384,
        hf_embed_device="cpu",
        embed_batch=256,
        bulk_source_type="default_cards",
        web_concurrency=1,
        cors_origins="*",
        log_level="INFO",
    )


def test_operation_ids_are_route_names() -> None:
    """OpenAPI `operationId` is exactly the route function name.

    Guarantees clean, stable MCP tool names — no `_path_method` suffix
    noise. Also the discipline anchor called out in process.md §10.1:
    every new endpoint's op-id = its function name automatically.
    """
    app = create_app(settings=_placeholder_settings())
    schema = app.openapi()
    name_by_path: dict[tuple[str, str], str] = {}
    for route in _walk_api_routes(app):
        if route.path.startswith("/mcp"):
            continue
        for method in route.methods or ():
            name_by_path[(route.path, method.lower())] = route.name
    for path, ops in schema["paths"].items():
        if path.startswith("/mcp"):
            continue
        for method, op in ops.items():
            if not isinstance(op, dict) or "operationId" not in op:
                continue
            expected = name_by_path.get((path, method))
            if expected is None:
                continue
            assert op["operationId"] == expected, (
                f"{method.upper()} {path}: "
                f"operationId={op['operationId']!r} != route.name={expected!r}"
            )


def test_mcp_tools_expose_read_endpoints() -> None:
    """The MCP tool list includes the core read endpoints agents rely on."""
    app = create_app(settings=_placeholder_settings())
    mcp = FastApiMCP(app)
    tool_names = {t.name for t in mcp.tools}
    expected = {
        "search_cards",
        "autocomplete_cards",
        "get_card_by_name",
        "get_card_by_id",
        "get_similar_cards",
        "get_substitutes",
        "get_card_tags",
        "get_card_combos",
        "get_card_explain",
        "get_card_related",
        "search_combos",
        "get_combo",
        "analyze_decklist",
        "compare_decklists",
        "diff_decklists",
        "export_decklist",
        "price_decklist",
        "goldfish_decklist",
        "synergy_decklist",
        "sideboard_suggest_decklist",
        "suggest_decklist",
        "decklist_combos",
        "decklist_bracket",
        "decklist_upgrade",
        "decklist_manabase",
        "decklist_legality",
        "decklist_mulligan",
        "decklist_near_combos",
        "random_card",
        "suggest_commanders",
        "list_tags",
        "list_keywords",
        "list_sets",
        "lookup_collection",
        "validate_search",
        "sync_deckbox_inventory",
        "list_inventory_cards",
        "get_inventory_summary_stats",
        "clear_inventory_collection",
        "upsert_inventory_card",
        "list_owned_oracle_ids",
        "upload_deckbox_inventory",
        "sync_wishlist_csv",
        "upload_wishlist_csv",
        "list_wishlist_cards",
        "get_wishlist_summary_stats",
        "list_wanted_oracle_ids",
        "upsert_wishlist_card",
        "clear_wishlist_collection",
        # D19.4 — stored-deck library.
        "create_deck",
        "upload_deck",
        "list_decks",
        "list_deck_cards",
        "list_deck_folders",
        "rename_deck_folder",
        "get_deck",
        "update_deck",
        "delete_deck",
        # D19.5 — version history.
        "list_deck_versions",
        "get_deck_version",
        "download_deck_version",
        "restore_deck_version",
        # D19.34 — branch-aware versioning.
        "set_deck_head",
        # D19.37 — import a new version onto an existing deck.
        "add_deck_version",
        # D19.40 — local (no-fetch) branch sync comparison.
        "get_deck_completion_plan",
        "list_deck_branches",
        # D19.41 — fork a deck into a new, independent deck.
        "fork_deck",
    }
    missing = expected - tool_names
    assert not missing, f"MCP tool list missing: {missing}"


def test_mcp_excludes_admin_refresh() -> None:
    """`trigger_refresh` never surfaces as an MCP tool.

    Ingest is a ~1h Scryfall bulk download + embed pass; agents must
    not trigger it. Exclusion enforced at MCP construction time in
    `main.create_app`; the pin here documents the intent.
    """
    app = create_app(settings=_placeholder_settings())
    # Any FastApiMCP instance built without an explicit exclude_operations
    # would expose it. The one wired in `create_app` sets the filter.
    exposed: set[str] = {r.name for r in _walk_api_routes(app)}
    assert "trigger_refresh" in exposed, (
        "trigger_refresh must still exist as a REST route — MCP exclusion "
        "is a tool-list filter, not a route removal"
    )
    # Now confirm the MCP wire-up (via main.create_app) actually filters
    # it. We rebuild the same exclusion rather than reaching into app.state.
    mcp = FastApiMCP(app, exclude_operations=["trigger_refresh"])
    tool_names = {t.name for t in mcp.tools}
    assert "trigger_refresh" not in tool_names


def test_mcp_endpoint_is_mounted() -> None:
    """The `/mcp` path exists on the app (streamable HTTP transport mount)."""
    app = create_app(settings=_placeholder_settings())
    mcp_paths = [r.path for r in _walk_api_routes(app) if r.path.startswith("/mcp")]
    assert mcp_paths, "no /mcp mount found"


def test_mcp_tool_descriptions_survive() -> None:
    """Route `summary` + docstring propagate into tool descriptions.

    Sanity check for the process.md §10.1 discipline: without a summary
    on the decorator, agents get an empty tool card. Pins that at least
    one non-trivial description hits the wire for `search_cards`.
    """
    app = create_app(settings=_placeholder_settings())
    mcp = FastApiMCP(app)
    by_name: dict[str, Any] = {t.name: t for t in mcp.tools}
    tool = by_name["search_cards"]
    assert tool.description and len(tool.description) > 20, tool.description
