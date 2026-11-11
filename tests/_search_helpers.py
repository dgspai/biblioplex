"""Shared fakes for ``GET /cards/search`` tests (M4).

The search endpoint speaks SQLAlchemy ``connect()/execute()`` and reads
rows via ``mappings().all()`` (each row is a ``raw`` JSONB + a ``total``
window-function count, per §6.1 / §3.2 — one round-trip via
``COUNT(*) OVER ()``). These fakes mimic that shape without touching a
DB so the router can be unit-tested end-to-end via :class:`ASGITransport`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex.config import Settings


class FakeResult:
    """Minimal stand-in for SQLAlchemy ``Result`` covering ``.mappings().all()``."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> FakeResult:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class FakeConn:
    """Capture the executed statement so tests can assert on rendered SQL."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.executed: list[tuple[Any, Any]] = []

    async def execute(self, stmt: Any, params: Any = None) -> FakeResult:
        self.executed.append((stmt, params))
        return FakeResult(self.rows)


class FakeConnCtx:
    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> FakeConn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeEngine:
    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn

    def connect(self) -> FakeConnCtx:
        return FakeConnCtx(self._conn)


def settings() -> Settings:
    """Settings shell — bypasses env requirements for unit-tests."""
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
    )


async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Async context-managed httpx client backed by the in-process app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def make_row(
    raw: dict[str, Any],
    total: int,
    predicted_labels: list[str] | None = None,
    label_scores: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Build a single fake search-row mapping (``raw`` + ``total`` columns).

    Step 5 added ``predicted_labels`` + ``label_scores`` to the search
    SELECT; default both to empty so existing fixtures stay terse.
    """
    return {
        "raw": raw,
        "total": total,
        "predicted_labels": predicted_labels or [],
        "label_scores": label_scores or {},
    }
