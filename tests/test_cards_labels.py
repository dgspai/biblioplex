"""Manual label override endpoints — PUT/DELETE /cards/{id}/labels.

Unit-level: a mocked engine captures the write and returns a canned
RETURNING row, so the contract under test is the endpoint's validation,
payload shape, and the `labels_overridden` flag — not Postgres semantics.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex.config import Settings
from biblioplex.main import create_app

_ID = "11111111-1111-1111-1111-111111111111"


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


@pytest.fixture
def app() -> FastAPI:
    return create_app(settings=_placeholder_settings())


def _engine(conn: AsyncMock) -> MagicMock:
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=None)
    engine = MagicMock()
    engine.connect.return_value = ctx
    engine.begin.return_value = ctx
    return engine


def _seq(*canned: Any) -> Any:
    it = iter(canned)

    def _next(*_a: Any, **_k: Any) -> Any:
        return next(it)

    return _next


async def _put(app: FastAPI, labels: list[str]) -> Any:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.put(f"/cards/{_ID}/labels", json={"labels": labels})


@pytest.mark.asyncio
async def test_put_pins_labels_and_flags_override(app: FastAPI) -> None:
    conn = AsyncMock()
    conn.execute.side_effect = _seq(
        AsyncMock(
            one_or_none=lambda: SimpleNamespace(
                raw={"name": "Sensei's Divining Top"},
                predicted_labels=["card_advantage"],
                label_scores={},
                label_reasons={"_override": ["card_advantage"]},
            )
        ),
    )
    app.state.read_engine = _engine(conn)

    resp = await _put(app, ["card_advantage"])

    assert resp.status_code == 200
    body = resp.json()
    assert body["predicted_labels"] == ["card_advantage"]
    assert body["labels_overridden"] is True


@pytest.mark.asyncio
async def test_put_rejects_unknown_label(app: FastAPI) -> None:
    # Validation happens before any DB work — no engine needed.
    app.state.read_engine = _engine(AsyncMock())
    resp = await _put(app, ["ramp", "bogus"])
    assert resp.status_code == 422
    assert "bogus" in resp.json()["details"]


@pytest.mark.asyncio
async def test_put_404_when_no_card(app: FastAPI) -> None:
    conn = AsyncMock()
    conn.execute.side_effect = _seq(AsyncMock(one_or_none=lambda: None))
    app.state.read_engine = _engine(conn)
    resp = await _put(app, ["ramp"])
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_reverts_to_rules_verdict(app: FastAPI) -> None:
    conn = AsyncMock()
    conn.execute.side_effect = _seq(
        # SELECT oracle_text/card_faces — a ramp rock, so rules say ramp.
        AsyncMock(
            one_or_none=lambda: SimpleNamespace(
                oracle_text="{T}: Add {G}{G}.", card_faces=None
            )
        ),
        # UPDATE ... RETURNING — override cleared, so label_reasons has no _override.
        AsyncMock(
            one=lambda: SimpleNamespace(
                raw={"name": "Some Rock"},
                predicted_labels=["ramp"],
                label_scores={"ramp": 1.0},
                label_reasons={"ramp": ["ramp:tap-add"]},
            )
        ),
    )
    app.state.read_engine = _engine(conn)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.delete(f"/cards/{_ID}/labels")

    assert resp.status_code == 200
    body = resp.json()
    assert body["predicted_labels"] == ["ramp"]
    assert body["labels_overridden"] is False
