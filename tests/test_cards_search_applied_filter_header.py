"""``X-Applied-Filter`` response header for `SEARCH_DEFAULT_FILTER` shadowing.

Surfaces the deployment-side DSL fragment that got AND-ed onto the user
query, so agents/operators can see the shadow without diffing the raw
URL against the rendered `q`.
"""

from __future__ import annotations

from fastapi import FastAPI

from biblioplex.config import Settings
from biblioplex.main import create_app
from biblioplex.routers.cards import get_read_engine

from ._search_helpers import FakeConn, FakeEngine, client, make_row


def _settings(default_filter: str = "") -> Settings:
    return Settings.model_construct(
        database_url="postgresql+psycopg://x/x",
        hf_embed_model="x",
        hf_embed_dim=384,
        hf_embed_device="cpu",
        embed_batch=256,
        bulk_source_type="default_cards",
        web_concurrency=1,
        cors_origins="*",
        log_level="INFO",
        search_default_filter=default_filter,
    )


def _override(app: FastAPI, conn: FakeConn) -> None:
    app.dependency_overrides[get_read_engine] = lambda: FakeEngine(conn)


async def test_header_absent_without_filter() -> None:
    app = create_app(settings=_settings(default_filter=""))
    _override(app, FakeConn(rows=[make_row({"name": "Bolt"}, total=1)]))
    async for c in client(app):
        resp = await c.get("/cards/search", params={"q": "bolt"})
    assert resp.status_code == 200
    assert "x-applied-filter" not in {k.lower() for k in resp.headers}


async def test_header_present_with_filter() -> None:
    app = create_app(settings=_settings(default_filter="f:commander"))
    _override(app, FakeConn(rows=[make_row({"name": "Bolt"}, total=1)]))
    async for c in client(app):
        resp = await c.get("/cards/search", params={"q": "bolt"})
    assert resp.status_code == 200
    assert resp.headers["x-applied-filter"] == "f:commander"
