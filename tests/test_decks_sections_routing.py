"""D19.9 — `/decks/*` reads sections, so headings stop being phantom cards.

Before this, every `/decks/*` endpoint called `parse_decklist`, which
treats each line as a card. A stored export carrying `Commander` /
`Mainboard` / `Sideboard` headings therefore sent those words to the
resolver and reported one phantom `unresolved` entry per heading —
`unresolved: ['Mainboard']` on the real Archidekt file, surfaced live.

The pins here key on the names actually handed to the resolver, which is
the tightest observable: a heading that never reaches SQL cannot become
an unresolved card, whatever the resolver returns.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex.config import Settings
from biblioplex.main import create_app
from biblioplex.routers.cards import get_read_engine

EXPORTS = Path(__file__).resolve().parent.parent / "data" / "example_deck_exports"


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


class _FakeConn:
    """Captures the names handed to the resolver; resolves nothing."""

    def __init__(self, commander_row: Any = None) -> None:
        self.resolved_names: list[str] = []
        self._commander = commander_row

    async def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        sql = str(stmt)
        if "unnest(" in sql and ":ords" in sql and params:
            self.resolved_names = list(params.get("names") or [])
            return _FakeResult([])
        if "color_identity" in sql and "LOWER(name)" in sql:
            return _FakeResult(
                [self._commander] if self._commander is not None else []
            )
        return _FakeResult([])


class _FakeConnCtx:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeEngine:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def connect(self) -> _FakeConnCtx:
        return _FakeConnCtx(self._conn)


def _settings() -> Settings:
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


@pytest.fixture
def app() -> FastAPI:
    return create_app(settings=_settings())


def _commander_row(name: str, ci: list[str]) -> Any:
    return SimpleNamespace(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        oracle_id=UUID("00000000-0000-0000-0000-000000000002"),
        name=name,
        color_identity=ci,
    )


async def _post(app: FastAPI, conn: _FakeConn, path: str, body: dict) -> Any:
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        return await ac.post(path, json=body)


# ---- the reported defect ----


@pytest.mark.asyncio
async def test_archidekt_headings_never_reach_the_resolver(app: FastAPI) -> None:
    """The live repro: `unresolved: ['Mainboard']` on a real export."""
    decklist = (EXPORTS / "archidekt" / "colorless_arsenal.txt").read_text()
    conn = _FakeConn()
    r = await _post(app, conn, "/decks/analyze", {"decklist": decklist})
    assert r.status_code == 200
    assert "Mainboard" not in conn.resolved_names
    assert "Commander" not in conn.resolved_names
    assert "Hope of Ghirapur" in conn.resolved_names
    # The fake conn resolves nothing, so everything lands in `unresolved`.
    # The defect was the HEADING appearing there, not the card names.
    assert "Mainboard" not in r.json()["unresolved"]


@pytest.mark.asyncio
async def test_arena_preamble_never_reaches_the_resolver(app: FastAPI) -> None:
    """Arena adds `About` / `Name <deck>` / `Deck` on top of `Commander`."""
    decklist = (EXPORTS / "moxfield" / "big-boys-arena.txt").read_text()
    conn = _FakeConn()
    r = await _post(app, conn, "/decks/analyze", {"decklist": decklist})
    assert r.status_code == 200
    for junk in ("About", "Name Big Boys", "Commander", "Deck"):
        assert junk not in conn.resolved_names


# ---- compatibility: a heading-less list is untouched ----


_PLAIN = "4 Lightning Bolt\n1 Sol Ring\n2 Llanowar Elves\n"
_PLAIN_NAMES = {"Lightning Bolt", "Sol Ring", "Llanowar Elves"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/decks/analyze",
        "/decks/price",
        "/decks/combos",
        "/decks/synergy",
        "/decks/mulligan",
        "/decks/bracket",
        "/decks/legality",
    ],
)
async def test_headingless_decklist_behaves_exactly_as_before(
    app: FastAPI, path: str
) -> None:
    """`main` holds everything when there are no headings.

    This is the property that keeps D19.9 from being a breaking change
    for every existing caller pasting a plain list.
    """
    conn = _FakeConn()
    r = await _post(app, conn, path, {"decklist": _PLAIN})
    assert r.status_code == 200
    assert set(conn.resolved_names) == _PLAIN_NAMES


# ---- per-endpoint bucket policy ----


_WITH_SIDEBOARD = (
    "1 Sol Ring\n"
    "Sideboard\n"
    "3 Grapeshot\n"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["/decks/analyze", "/decks/combos", "/decks/synergy", "/decks/mulligan"],
)
async def test_sideboard_is_excluded_from_maindeck_analysis(
    app: FastAPI, path: str
) -> None:
    """Label counts, curve and simulations mean the MAINDECK.

    A sideboard Grapeshot must not inflate the deck's plan count or get
    shuffled into an opening hand.
    """
    conn = _FakeConn()
    r = await _post(app, conn, path, {"decklist": _WITH_SIDEBOARD})
    assert r.status_code == 200
    assert conn.resolved_names == ["Sol Ring"]


@pytest.mark.asyncio
async def test_price_includes_the_sideboard(app: FastAPI) -> None:
    """You buy the sideboard too — deck cost spans main + sideboard."""
    conn = _FakeConn()
    r = await _post(app, conn, "/decks/price", {"decklist": _WITH_SIDEBOARD})
    assert r.status_code == 200
    assert set(conn.resolved_names) == {"Sol Ring", "Grapeshot"}


@pytest.mark.asyncio
async def test_export_round_trips_main_and_sideboard(app: FastAPI) -> None:
    """Export is a format converter — it must not silently drop cards."""
    conn = _FakeConn()
    r = await _post(
        app, conn, "/decks/export", {"decklist": _WITH_SIDEBOARD, "style": "plain"}
    )
    assert r.status_code == 200
    assert r.json()["total_qty"] == 4
    assert "Sideboard" not in r.json()["decklist"]


# ---- legality: the commander must not be counted twice ----


_COMMANDER_DECK = "Commander\n1 Hope of Ghirapur\n\nDeck\n99 Sol Ring\n"


@pytest.mark.asyncio
async def test_legality_excludes_the_parsed_commander_from_the_body(
    app: FastAPI,
) -> None:
    """`main` contains the commander (D19.10); the legality body must not.

    `check_legality` expects 99 when a commander CI is supplied. Feeding
    it `main` unmodified would hand it 100 and report a bogus deck_size
    violation on a perfectly legal deck.
    """
    conn = _FakeConn(commander_row=_commander_row("Kenrith", ["W", "U", "B"]))
    r = await _post(app, conn, "/decks/legality", {"decklist": _COMMANDER_DECK})
    assert r.status_code == 200
    body = r.json()
    assert body["deck_size"] == 99
    assert body["expected_deck_size"] == 100
    assert [v for v in body["violations"] if v["reason"] == "deck_size"] == []
    assert body["commander_color_identity"] == ["W", "U", "B"]


@pytest.mark.asyncio
async def test_legality_explicit_commander_wins_over_the_parsed_one(
    app: FastAPI,
) -> None:
    """Explicit beats inferred — the body param is the caller's override."""
    conn = _FakeConn(commander_row=_commander_row("Yorion, Sky Nomad", ["W", "U"]))
    r = await _post(
        app,
        conn,
        "/decks/legality",
        {"decklist": _COMMANDER_DECK, "commander": "Yorion, Sky Nomad"},
    )
    assert r.status_code == 200
    assert r.json()["commander_color_identity"] == ["W", "U"]


@pytest.mark.asyncio
async def test_legality_inline_hundred_still_accepted(app: FastAPI) -> None:
    """No `Commander` section and no param — the whole 100 is the body."""
    conn = _FakeConn()
    # Two lines: `100 Sol Ring` would clamp to 99 at the parser's qty cap.
    r = await _post(
        app, conn, "/decks/legality", {"decklist": "60 Sol Ring\n40 Mana Crypt\n"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["deck_size"] == 100
    assert [v for v in body["violations"] if v["reason"] == "deck_size"] == []


@pytest.mark.asyncio
async def test_bracket_uses_the_parsed_commander(app: FastAPI) -> None:
    """`/decks/bracket` takes a commander separately, same as legality."""
    conn = _FakeConn(commander_row=_commander_row("Hope of Ghirapur", []))
    r = await _post(app, conn, "/decks/bracket", {"decklist": _COMMANDER_DECK})
    assert r.status_code == 200
    assert "Commander" not in conn.resolved_names


@pytest.mark.asyncio
async def test_colorless_inferred_commander_reports_null_identity(
    app: FastAPI,
) -> None:
    """Pre-existing wart, pinned because inference makes it reachable.

    `legality.py` builds the field as `list(ci) if commander_ci else None`,
    so a COLORLESS commander reports `null` — indistinguishable from "no
    commander was supplied". Harmless before D19.9 (the caller knew what
    it passed); now a `Commander` heading can supply one implicitly, so the
    response no longer reveals that a commander was used at all. The body
    count still proves it: 99, not 100. Left as-is rather than fixed here,
    since changing it alters an existing documented response. See the
    `Spec Drift / M19` entry.
    """
    conn = _FakeConn(commander_row=_commander_row("Hope of Ghirapur", []))
    r = await _post(app, conn, "/decks/legality", {"decklist": _COMMANDER_DECK})
    assert r.status_code == 200
    body = r.json()
    assert body["commander_color_identity"] is None
    assert body["deck_size"] == 99
