"""MCP tool annotations: clients can tell reads from writes from deletes."""

from __future__ import annotations

from fastapi_mcp import FastApiMCP

from biblioplex.main import (
    MCP_EXCLUDED_OPERATIONS,
    MCP_READONLY_POSTS,
    _annotate_mcp_tools,
    create_app,
)

from .test_mcp_mount import _placeholder_settings


def _annotated() -> tuple[FastApiMCP, dict[str, dict[str, object]]]:
    mcp = FastApiMCP(
        create_app(settings=_placeholder_settings()),
        exclude_operations=MCP_EXCLUDED_OPERATIONS,
    )
    _annotate_mcp_tools(mcp)
    hints = {
        t.name: t.annotations.model_dump(exclude_none=True)
        for t in mcp.tools
        if t.annotations is not None
    }
    return mcp, hints


def test_every_tool_is_annotated_by_http_method() -> None:
    mcp, hints = _annotated()
    assert set(hints) == {t.name for t in mcp.tools}
    for name, h in hints.items():
        method = mcp.operation_map[name]["method"].lower()
        if method == "get":
            assert h == {"readOnlyHint": True}, name
        elif method == "delete":
            assert h == {"readOnlyHint": False, "destructiveHint": True}, name
        elif name in MCP_READONLY_POSTS:
            assert method == "post" and h == {"readOnlyHint": True}, name
        else:
            assert h == {"readOnlyHint": False}, name


def test_readonly_posts_are_real_tools() -> None:
    """A renamed route must not silently drop out of the read-only set."""
    mcp, _ = _annotated()
    assert {t.name for t in mcp.tools} >= MCP_READONLY_POSTS


def test_known_tools() -> None:
    _, hints = _annotated()
    assert hints["search_cards"] == {"readOnlyHint": True}
    assert hints["get_card_printings"] == {"readOnlyHint": True}
    assert hints["analyze_decklist"] == {"readOnlyHint": True}
    assert hints["delete_deck"]["destructiveHint"] is True
    assert hints["clear_inventory_collection"]["destructiveHint"] is True
    assert hints["clear_wishlist_collection"]["destructiveHint"] is True
    assert hints["upsert_inventory_card"] == {"readOnlyHint": False}
    assert hints["create_deck"] == {"readOnlyHint": False}
