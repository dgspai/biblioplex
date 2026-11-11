"""D10.8 — re-check ``q`` length AFTER ``SEARCH_DEFAULT_FILTER`` wrap (§6.1).

The user-side 1000-char cap in ``search_cards`` runs before the deployment
filter is AND-ed on. Without a post-wrap re-check, a 1000-char ``q`` plus
a non-empty ``SEARCH_DEFAULT_FILTER`` silently produces a parser input
that exceeds the bound the cap exists to enforce.

Tests pinned to Shape B (two checks):

* The pre-wrap cap fires unchanged with the message ``q too long``
  (preserves the existing :mod:`tests.test_cards_search_d4_3` assertion).
* The post-wrap check fires only when the wrap pushed an otherwise-valid
  ``q`` over the bound and carries a filter-aware ``details`` so an
  operator debugging it can tell the deployment config is the cause.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from biblioplex.config import Settings
from biblioplex.main import create_app
from biblioplex.routers.cards import SEARCH_Q_MAX_LEN, get_read_engine

from ._search_helpers import FakeConn, FakeEngine, client


def _settings(default_filter: str = "") -> Settings:
    """Settings shell with an explicit ``search_default_filter`` override."""
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


# ---- regressions: existing behavior preserved ----


async def test_pre_wrap_cap_unchanged_without_default_filter() -> None:
    """User ``q`` of 1001 chars with no filter → 422 ``q too long`` (regression)."""
    app = create_app(settings=_settings(default_filter=""))
    _override(app, FakeConn(rows=[]))
    async for c in client(app):
        resp = await c.get("/cards/search", params={"q": "a" * 1001})

    assert resp.status_code == 422
    body = resp.json()
    assert body["object"] == "error"
    assert body["details"] == "q too long"


async def test_q_at_cap_with_empty_filter_passes_validation() -> None:
    """1000-char ``q`` with empty filter still passes the cap (regression)."""
    app = create_app(settings=_settings(default_filter=""))
    _override(app, FakeConn(rows=[]))
    async for c in client(app):
        resp = await c.get("/cards/search", params={"q": "a" * SEARCH_Q_MAX_LEN})

    # 200 (rows) or 404 (FakeConn returns no rows → total=0) — either proves
    # length validation passed, which is what this regression pins.
    assert resp.status_code in (200, 404)


async def test_pre_wrap_cap_fires_first_when_user_q_oversized() -> None:
    """Shape B: with filter set, a 1001-char user ``q`` still trips the
    pre-wrap cap with the plain ``q too long`` message — the filter never
    gets to inflate the query because we bail before the wrap.
    """
    app = create_app(settings=_settings(default_filter="f:commander"))
    _override(app, FakeConn(rows=[]))
    async for c in client(app):
        resp = await c.get("/cards/search", params={"q": "a" * 1001})

    assert resp.status_code == 422
    body = resp.json()
    assert body["details"] == "q too long"


# ---- new: post-wrap re-check fires when the wrap inflates over the cap ----


async def test_post_wrap_cap_fires_with_filter_aware_message() -> None:
    """User ``q`` within the cap + a long ``SEARCH_DEFAULT_FILTER`` that
    pushes the rendered query over the cap → 422 with a filter-aware
    message distinct from the plain ``q too long``.
    """
    # 900 chars of user `q` + ("f:commander " * 20) = 240 chars of filter →
    # wrapped length ≈ 900 + 2 + 1 + 239 ≈ 1142, comfortably over 1000.
    long_filter = ("f:commander " * 20).strip()
    app = create_app(settings=_settings(default_filter=long_filter))
    _override(app, FakeConn(rows=[]))
    async for c in client(app):
        resp = await c.get("/cards/search", params={"q": "a" * 900})

    assert resp.status_code == 422
    body = resp.json()
    assert body["object"] == "error"
    assert body["status"] == 422
    # Distinguishable from the plain "q too long" pre-wrap branch — must
    # reference SEARCH_DEFAULT_FILTER so an operator can see the deployment
    # config is the cause, not the client.
    details = body["details"]
    assert details != "q too long"
    assert "SEARCH_DEFAULT_FILTER" in details


async def test_wrapped_q_under_cap_still_succeeds() -> None:
    """500-char ``q`` + ``f:commander`` (~515 chars wrapped) → still under
    the cap, validation passes.
    """
    app = create_app(settings=_settings(default_filter="f:commander"))
    _override(app, FakeConn(rows=[]))
    async for c in client(app):
        resp = await c.get("/cards/search", params={"q": "a" * 500})

    # 200 or 404 — either proves length validation passed.
    assert resp.status_code in (200, 404)


# Belt-and-suspenders: pin the spec constant so a future change to
# SEARCH_Q_MAX_LEN cascades into a failed assertion here (forcing the
# tester to revisit the wrap-length budget).
def test_spec_cap_is_1000() -> None:
    """§6.1 pins ``q`` at ≤1000 chars."""
    assert SEARCH_Q_MAX_LEN == 1000


# Silence the "unused import" pylint warning under pytest's
# fixture-collection model — `pytest` is imported because future
# parametrized cases under this file will need it.
_ = pytest
