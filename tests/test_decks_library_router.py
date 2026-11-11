"""D19.4 — /decks CRUD HTTP surface for stored decks."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex.config import Settings
from biblioplex.main import create_app
from biblioplex.routers.decks._common import DECKLIST_MAX_LEN

EXPORTS = Path(__file__).resolve().parent.parent / "data" / "example_deck_exports"


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
    a = create_app(settings=_placeholder_settings())
    a.state.read_engine = _mock_engine()
    return a


def _mock_engine() -> MagicMock:
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=AsyncMock())
    ctx.__aexit__ = AsyncMock(return_value=None)
    engine = MagicMock()
    engine.connect.return_value = ctx
    engine.begin.return_value = ctx
    return engine


DECK = {
    "object": "deck",
    "id": "11111111-1111-1111-1111-111111111111",
    "name": "Iroh gift",
    "format": "commander",
    "commander": None,
    "companion": None,
    "version": 1,
    "version_created": True,
    "total_cards": 100,
}
UUID_A = "11111111-1111-1111-1111-111111111111"


def _svc(name: str, **kw: Any) -> Any:
    return patch(f"biblioplex.routers.deck_library.deck_service.{name}", **kw)


async def _post(app: FastAPI, url: str, **kw: Any) -> Any:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.post(url, **kw)


async def _patch(app: FastAPI, url: str, **kw: Any) -> Any:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.patch(url, **kw)


# ---- create ----


@pytest.mark.asyncio
async def test_create_deck_returns_the_deck(app: FastAPI) -> None:
    with _svc("create_deck", new=AsyncMock(return_value=DECK)):
        resp = await _post(app, "/decks", json={"name": "Iroh gift", "decklist": "1 Sol Ring\n"})
    assert resp.status_code == 200
    assert resp.json()["object"] == "deck"


@pytest.mark.asyncio
async def test_create_deck_uses_a_write_transaction(app: FastAPI) -> None:
    """`connect()` opens a transaction that ROLLS BACK on close (D18.3)."""
    with _svc("create_deck", new=AsyncMock(return_value=DECK)):
        await _post(app, "/decks", json={"decklist": "1 Sol Ring\n"})
    app.state.read_engine.begin.assert_called_once()
    app.state.read_engine.connect.assert_not_called()


@pytest.mark.asyncio
async def test_create_deck_records_source_paste(app: FastAPI) -> None:
    with _svc("create_deck", new=AsyncMock(return_value=DECK)) as m:
        await _post(app, "/decks", json={"decklist": "1 Sol Ring\n"})
    assert m.await_args.kwargs["source"] == "paste"
    assert m.await_args.kwargs["raw_content"] is None


@pytest.mark.asyncio
async def test_create_deck_rejects_an_empty_decklist(app: FastAPI) -> None:
    resp = await _post(app, "/decks", json={"decklist": "   "})
    assert resp.status_code == 422
    assert resp.json()["code"] == "bad_request"


@pytest.mark.asyncio
async def test_create_deck_rejects_an_oversized_decklist(app: FastAPI) -> None:
    """The cap is enforced at WRITE time so the failure lands once,
    not on every later /decks/analyze of a deck that can never be analysed."""
    resp = await _post(
        app, "/decks", json={"decklist": "1 Sol Ring\n" * (DECKLIST_MAX_LEN // 10)}
    )
    assert resp.status_code == 422
    assert "too long" in resp.json()["details"]


@pytest.mark.asyncio
async def test_create_deck_accepts_a_bulk_collection_list(app: FastAPI) -> None:
    """A ~22K-char bulk list ("white lotus all", 2000 lines) is what the
    Moxfield sync pushes; the old 8000-char cap turned that into a 422."""
    with _svc("create_deck", new=AsyncMock(return_value=DECK)):
        resp = await _post(app, "/decks", json={"decklist": "1 Sol Ring\n" * 2000})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_create_deck_rejects_a_url_on_the_wrong_service(app: FastAPI) -> None:
    """A Moxfield URL in `archidekt_url` would silently break D14.18's pull."""
    resp = await _post(app, "/decks", json={
        "decklist": "1 Sol Ring\n",
        "archidekt_url": "https://moxfield.com/decks/abc",
    })
    assert resp.status_code == 422
    assert resp.json()["details"] == "archidekt_url must point at archidekt.com"


@pytest.mark.asyncio
async def test_create_deck_accepts_a_scheme_less_url(app: FastAPI) -> None:
    with _svc("create_deck", new=AsyncMock(return_value=DECK)) as m:
        resp = await _post(app, "/decks", json={
            "decklist": "1 Sol Ring\n",
            "moxfield_url": "moxfield.com/decks/abc",
        })
    assert resp.status_code == 200
    assert m.await_args.kwargs["moxfield_url"] == "moxfield.com/decks/abc"


@pytest.mark.asyncio
async def test_create_deck_accepts_a_subdomain(app: FastAPI) -> None:
    with _svc("create_deck", new=AsyncMock(return_value=DECK)):
        resp = await _post(app, "/decks", json={
            "decklist": "1 Sol Ring\n",
            "deckbox_url": "https://www.deckbox.org/sets/123",
        })
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_create_deck_derives_the_name_from_an_arena_about_block(app: FastAPI) -> None:
    """D19.2 already recovers `Name Big Boys`; do not make the user retype it."""
    raw = (EXPORTS / "moxfield" / "big-boys-arena.txt").read_text()
    with _svc("create_deck", new=AsyncMock(return_value=DECK)) as m:
        await _post(app, "/decks", json={"decklist": raw})
    assert m.await_args.kwargs["name"] == "Big Boys"


@pytest.mark.asyncio
async def test_create_deck_falls_back_to_a_placeholder_name(app: FastAPI) -> None:
    with _svc("create_deck", new=AsyncMock(return_value=DECK)) as m:
        await _post(app, "/decks", json={"decklist": "1 Sol Ring\n"})
    assert m.await_args.kwargs["name"] == "Untitled deck"


@pytest.mark.asyncio
async def test_create_deck_coerces_pasted_csv(app: FastAPI) -> None:
    with _svc("create_deck", new=AsyncMock(return_value=DECK)) as m:
        await _post(app, "/decks", json={"decklist": "Count,Name,Section\n1,Sol Ring,main\n"})
    assert m.await_args.kwargs["decklist"] == "1 Sol Ring\n"


# ---- upload ----


@pytest.mark.asyncio
async def test_upload_deck_keeps_raw_bytes_and_coerces_the_text(app: FastAPI) -> None:
    csv_path = EXPORTS / "deckbox" / "Iroh gift_2026.August.18.csv"
    raw = csv_path.read_bytes()
    with _svc("create_deck", new=AsyncMock(return_value=DECK)) as m:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.post(
                "/decks/upload",
                files={"file": (csv_path.name, raw, "text/csv")},
            )
    assert resp.status_code == 200
    kw = m.await_args.kwargs
    assert kw["source"] == "upload"
    assert kw["raw_content"] == raw
    assert kw["filename"] == csv_path.name
    assert kw["decklist"].startswith("1 Ash Barrens")
    assert "Sideboard" in kw["decklist"]


@pytest.mark.asyncio
async def test_upload_deck_names_the_deck_from_the_filename(app: FastAPI) -> None:
    with _svc("create_deck", new=AsyncMock(return_value=DECK)) as m:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            await c.post("/decks/upload", files={"file": ("my-deck.txt", b"1 Sol Ring\n", "text/plain")})
    assert m.await_args.kwargs["name"] == "my-deck"


@pytest.mark.asyncio
async def test_upload_deck_uses_a_write_transaction(app: FastAPI) -> None:
    with _svc("create_deck", new=AsyncMock(return_value=DECK)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            await c.post("/decks/upload", files={"file": ("d.txt", b"1 Sol Ring\n", "text/plain")})
    app.state.read_engine.begin.assert_called_once()
    app.state.read_engine.connect.assert_not_called()


@pytest.mark.asyncio
async def test_upload_deck_rejects_an_empty_file(app: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/decks/upload", files={"file": ("d.txt", b"   ", "text/plain")})
    assert resp.status_code == 422


# ---- read ----


@pytest.mark.asyncio
async def test_list_decks_uses_a_read_connection(app: FastAPI) -> None:
    with _svc("list_decks", new=AsyncMock(return_value={"object": "list", "data": []})):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get("/decks")
    assert resp.status_code == 200
    app.state.read_engine.connect.assert_called_once()
    app.state.read_engine.begin.assert_not_called()


@pytest.mark.asyncio
async def test_get_deck_404s_on_an_unknown_id(app: FastAPI) -> None:
    with _svc("get_deck", new=AsyncMock(return_value=None)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get(f"/decks/{UUID_A}")
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


@pytest.mark.asyncio
async def test_get_deck_404s_on_a_malformed_id_without_touching_the_db(app: FastAPI) -> None:
    """`CAST('nope' AS uuid)` is a DB-level error — reject before the query."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/decks/not-a-uuid")
    assert resp.status_code == 404
    app.state.read_engine.connect.assert_not_called()


# ---- evaluate ----


@pytest.mark.asyncio
async def test_evaluate_deck_404s_on_malformed_id_without_touching_the_db(
    app: FastAPI,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/decks/not-a-uuid/evaluate")
    assert resp.status_code == 404
    app.state.read_engine.connect.assert_not_called()


@pytest.mark.asyncio
async def test_evaluate_deck_404s_on_unknown_id(app: FastAPI) -> None:
    with _svc("get_deck", new=AsyncMock(return_value=None)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get(f"/decks/{UUID_A}/evaluate")
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


@pytest.mark.asyncio
async def test_evaluate_deck_returns_summary_and_tips(app: FastAPI) -> None:
    """HEAD decklist → analyze pipeline → tips engine, in one payload."""
    deck = {"name": "Krenko", "decklist": "1 Sol Ring\n33 Mountain\n"}
    analysis = {
        "object": "deck_analysis",
        "counts": {
            "ramp": 1,
            "card_advantage": 0,
            "targeted_disrupt": 0,
            "mass_disrupt": 0,
            "plan": 0,
            "land": 33,
        },
        "targets": {
            "ramp": 12,
            "card_advantage": 12,
            "targeted_disrupt": 12,
            "mass_disrupt": 6,
            "plan": 30,
        },
        "deltas": {
            "ramp": -11,
            "card_advantage": -12,
            "targeted_disrupt": -12,
            "mass_disrupt": -6,
            "plan": -30,
        },
        "curve": {"0": 0, "1": 1, "2": 0, "3": 0, "4": 0, "5": 0, "6": 0, "7+": 0},
        "curve_targets": {"1": 9, "2": 18, "3": 15, "4": 10, "5": 5, "6+": 5},
        "curve_deltas": {"1": -8, "2": -18, "3": -15, "4": -10, "5": -5, "6+": -5},
        "pips": {},
        "cards": [],
        "unresolved": [],
        "inventory_summary": None,
        "missing_cards": [],
    }
    with _svc("get_deck", new=AsyncMock(return_value=deck)), patch(
        "biblioplex.routers.deck_library._analyze_pipeline",
        new=AsyncMock(return_value=analysis),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.get(f"/decks/{UUID_A}/evaluate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "deck_evaluation"
    assert body["deck_id"] == UUID_A
    assert body["name"] == "Krenko"
    codes = {t["code"] for t in body["tips"]}
    assert "low_lands" in codes
    assert "under_ramp" in codes
    # Full analyze payload rides along so the client renders curve/slots.
    assert body["analysis"]["curve_targets"]["2"] == 18


# ---- update / delete ----


@pytest.mark.asyncio
async def test_update_deck_uses_a_write_transaction(app: FastAPI) -> None:
    with _svc("update_deck", new=AsyncMock(return_value=DECK)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.patch(f"/decks/{UUID_A}", json={"notes": "gift"})
    assert resp.status_code == 200
    app.state.read_engine.begin.assert_called_once()
    app.state.read_engine.connect.assert_not_called()


@pytest.mark.asyncio
async def test_update_deck_marks_a_decklist_change_as_an_edit(app: FastAPI) -> None:
    with _svc("update_deck", new=AsyncMock(return_value=DECK)) as m:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            await c.patch(f"/decks/{UUID_A}", json={"decklist": "2 Sol Ring\n"})
    assert m.await_args.kwargs["source"] == "edit"


@pytest.mark.asyncio
async def test_update_deck_only_forwards_supplied_fields(app: FastAPI) -> None:
    with _svc("update_deck", new=AsyncMock(return_value=DECK)) as m:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            await c.patch(f"/decks/{UUID_A}", json={"notes": "gift"})
    assert m.await_args.kwargs["fields"] == {"notes": "gift"}


@pytest.mark.asyncio
async def test_update_deck_404s_on_an_unknown_id(app: FastAPI) -> None:
    with _svc("update_deck", new=AsyncMock(return_value=None)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.patch(f"/decks/{UUID_A}", json={"notes": "x"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_deck_uses_a_write_transaction(app: FastAPI) -> None:
    with _svc("delete_deck", new=AsyncMock(return_value=True)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.delete(f"/decks/{UUID_A}")
    assert resp.status_code == 200
    assert resp.json()["object"] == "deck_deleted"
    app.state.read_engine.begin.assert_called_once()
    app.state.read_engine.connect.assert_not_called()


@pytest.mark.asyncio
async def test_delete_deck_404s_when_nothing_went(app: FastAPI) -> None:
    with _svc("delete_deck", new=AsyncMock(return_value=False)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.delete(f"/decks/{UUID_A}")
    assert resp.status_code == 404


# ---- the stateless /decks/* surface must still work ----


@pytest.mark.asyncio
async def test_storage_routes_do_not_shadow_decks_analyze(app: FastAPI) -> None:
    """`POST /decks/upload` and `POST /decks/analyze` are distinct literals;
    a regression here would break every existing decklist endpoint."""
    resp = await _post(app, "/decks/analyze", json={"decklist": ""})
    assert resp.status_code == 422
    assert resp.json()["details"].startswith("decklist")


# ---- D19.11 companion ----


@pytest.mark.asyncio
async def test_create_deck_forwards_the_companion_override(app: FastAPI) -> None:
    with _svc("create_deck", new=AsyncMock(return_value=DECK)) as m:
        await _post(app, "/decks", json={
            "decklist": "1 Sol Ring\n",
            "companion": "Yorion, Sky Nomad",
        })
    assert m.await_args.kwargs["companion"] == "Yorion, Sky Nomad"


@pytest.mark.asyncio
async def test_patch_deck_forwards_the_companion_override(app: FastAPI) -> None:
    with _svc("update_deck", new=AsyncMock(return_value=DECK)) as m:
        await _patch(app, f"/decks/{DECK['id']}", json={"companion": "Lurrus of the Dream-Den"})
    assert m.await_args.kwargs["fields"]["companion"] == "Lurrus of the Dream-Den"


# ---- D19.34: branch + message, promote head ----


@pytest.mark.asyncio
async def test_create_deck_forwards_branch_and_message(app: FastAPI) -> None:
    with _svc("create_deck", new=AsyncMock(return_value=DECK)) as m:
        await _post(app, "/decks", json={
            "decklist": "1 Sol Ring\n", "branch": "deckbox", "message": "first import",
        })
    assert m.await_args.kwargs["branch"] == "deckbox"
    assert m.await_args.kwargs["message"] == "first import"


@pytest.mark.asyncio
async def test_create_deck_defaults_branch_to_main(app: FastAPI) -> None:
    with _svc("create_deck", new=AsyncMock(return_value=DECK)) as m:
        await _post(app, "/decks", json={"decklist": "1 Sol Ring\n"})
    assert m.await_args.kwargs["branch"] == "main"


@pytest.mark.asyncio
async def test_create_deck_rejects_an_unknown_branch(app: FastAPI) -> None:
    resp = await _post(app, "/decks", json={"decklist": "1 Sol Ring\n", "branch": "nope"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "bad_request"


@pytest.mark.asyncio
async def test_upload_deck_forwards_branch_and_message(app: FastAPI) -> None:
    with _svc("create_deck", new=AsyncMock(return_value=DECK)) as m:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            await c.post(
                "/decks/upload",
                files={"file": ("d.txt", b"1 Sol Ring\n", "text/plain")},
                data={"branch": "moxfield", "message": "moxfield sync"},
            )
    assert m.await_args.kwargs["branch"] == "moxfield"
    assert m.await_args.kwargs["message"] == "moxfield sync"


@pytest.mark.asyncio
async def test_upload_deck_rejects_an_unknown_branch(app: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            "/decks/upload",
            files={"file": ("d.txt", b"1 Sol Ring\n", "text/plain")},
            data={"branch": "nope"},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_deck_forwards_branch_and_message_with_a_decklist(app: FastAPI) -> None:
    with _svc("update_deck", new=AsyncMock(return_value=DECK)) as m:
        await _patch(
            app, f"/decks/{DECK['id']}",
            json={"decklist": "2 Sol Ring\n", "branch": "archidekt", "message": "resync"},
        )
    assert m.await_args.kwargs["branch"] == "archidekt"
    assert m.await_args.kwargs["message"] == "resync"


@pytest.mark.asyncio
async def test_patch_deck_rejects_an_unknown_branch_only_when_a_decklist_is_supplied(
    app: FastAPI,
) -> None:
    resp = await _patch(
        app, f"/decks/{DECK['id']}", json={"decklist": "1 Sol Ring\n", "branch": "nope"}
    )
    assert resp.status_code == 422
    # A metadata-only PATCH never touches a version, so an invalid branch
    # there is inert rather than an error.
    with _svc("update_deck", new=AsyncMock(return_value=DECK)):
        resp2 = await _patch(app, f"/decks/{DECK['id']}", json={"notes": "x", "branch": "nope"})
    assert resp2.status_code == 200


@pytest.mark.asyncio
async def test_patch_deck_rejects_an_invalid_trunk_branch(app: FastAPI) -> None:
    resp = await _patch(app, f"/decks/{DECK['id']}", json={"trunk_branch": "nope"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "bad_request"


@pytest.mark.asyncio
async def test_patch_deck_forwards_trunk_branch(app: FastAPI) -> None:
    with _svc("update_deck", new=AsyncMock(return_value=DECK)) as m:
        await _patch(app, f"/decks/{DECK['id']}", json={"trunk_branch": "main"})
    assert m.await_args.kwargs["fields"]["trunk_branch"] == "main"


@pytest.mark.asyncio
async def test_patch_deck_clears_trunk_branch_back_to_null(app: FastAPI) -> None:
    """`trunk_branch: null` must reach the service as `None`, not be dropped —
    that is the only spelling that resumes following every import."""
    with _svc("update_deck", new=AsyncMock(return_value=DECK)) as m:
        await _patch(app, f"/decks/{DECK['id']}", json={"trunk_branch": None})
    assert m.await_args.kwargs["fields"]["trunk_branch"] is None


@pytest.mark.asyncio
async def test_set_deck_head_promotes_and_pins(app: FastAPI) -> None:
    row = {"version": 3, "branch": "deckbox", "head_pinned": True}
    with _svc("set_head", new=AsyncMock(return_value=row)) as m:
        resp = await _post(app, f"/decks/{UUID_A}/head", json={"version": 3})
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "deck_head"
    assert body["version"] == 3
    assert body["branch"] == "deckbox"
    assert body["head_pinned"] is True
    assert m.await_args.args[2] == 3


@pytest.mark.asyncio
async def test_set_deck_head_uses_a_write_transaction(app: FastAPI) -> None:
    with _svc("set_head", new=AsyncMock(return_value={"version": 1, "branch": "main", "head_pinned": True})):
        await _post(app, f"/decks/{UUID_A}/head", json={"version": 1})
    app.state.read_engine.begin.assert_called_once()
    app.state.read_engine.connect.assert_not_called()


@pytest.mark.asyncio
async def test_set_deck_head_404s_on_an_unknown_version(app: FastAPI) -> None:
    with _svc("set_head", new=AsyncMock(return_value=None)):
        resp = await _post(app, f"/decks/{UUID_A}/head", json={"version": 99})
    assert resp.status_code == 404


# ---- D19.37: un-pin via version: null ----


@pytest.mark.asyncio
async def test_set_deck_head_null_version_clears_the_pin(app: FastAPI) -> None:
    row = {"version": 4, "branch": "main", "head_pinned": False}
    with _svc("set_head", new=AsyncMock(return_value=row)) as m:
        resp = await _post(app, f"/decks/{UUID_A}/head", json={"version": None})
    assert resp.status_code == 200
    body = resp.json()
    assert body["head_pinned"] is False
    assert body["version"] == 4
    assert m.await_args.args[2] is None


@pytest.mark.asyncio
async def test_set_deck_head_omitted_version_also_clears_the_pin(app: FastAPI) -> None:
    row = {"version": 4, "branch": "main", "head_pinned": False}
    with _svc("set_head", new=AsyncMock(return_value=row)) as m:
        resp = await _post(app, f"/decks/{UUID_A}/head", json={})
    assert resp.status_code == 200
    assert m.await_args.args[2] is None


@pytest.mark.asyncio
async def test_set_deck_head_unpin_404s_on_a_deck_with_no_versions(app: FastAPI) -> None:
    with _svc("set_head", new=AsyncMock(return_value=None)):
        resp = await _post(app, f"/decks/{UUID_A}/head", json={"version": None})
    assert resp.status_code == 404


# ---- D19.37: import a new version onto an existing deck ----


@pytest.mark.asyncio
async def test_add_deck_version_uses_a_write_transaction(app: FastAPI) -> None:
    with _svc("update_deck", new=AsyncMock(return_value=DECK)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.post(
                f"/decks/{UUID_A}/versions",
                files={"file": ("d.txt", b"1 Sol Ring\n", "text/plain")},
            )
    assert resp.status_code == 200
    app.state.read_engine.begin.assert_called_once()
    app.state.read_engine.connect.assert_not_called()


@pytest.mark.asyncio
async def test_add_deck_version_forwards_file_branch_message(app: FastAPI) -> None:
    with _svc("update_deck", new=AsyncMock(return_value=DECK)) as m:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.post(
                f"/decks/{UUID_A}/versions",
                files={"file": ("d.txt", b"1 Sol Ring\n", "text/plain")},
                data={"branch": "moxfield", "message": "resync"},
            )
    assert resp.status_code == 200
    kw = m.await_args.kwargs
    assert kw["decklist"] == "1 Sol Ring\n"
    assert kw["source"] == "upload"
    assert kw["branch"] == "moxfield"
    assert kw["message"] == "resync"
    assert kw["filename"] == "d.txt"
    assert kw["raw_content"] == b"1 Sol Ring\n"
    assert m.await_args.args[1] == UUID_A


@pytest.mark.asyncio
async def test_add_deck_version_defaults_source_to_upload(app: FastAPI) -> None:
    with _svc("update_deck", new=AsyncMock(return_value=DECK)) as m:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            await c.post(
                f"/decks/{UUID_A}/versions",
                files={"file": ("d.txt", b"1 Sol Ring\n", "text/plain")},
            )
    assert m.await_args.kwargs["source"] == "upload"


@pytest.mark.asyncio
async def test_add_deck_version_rejects_an_unknown_branch(app: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            f"/decks/{UUID_A}/versions",
            files={"file": ("d.txt", b"1 Sol Ring\n", "text/plain")},
            data={"branch": "nope"},
        )
    assert resp.status_code == 422
    assert resp.json()["code"] == "bad_request"


@pytest.mark.asyncio
async def test_add_deck_version_rejects_an_unknown_source(app: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            f"/decks/{UUID_A}/versions",
            files={"file": ("d.txt", b"1 Sol Ring\n", "text/plain")},
            data={"source": "nope"},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_add_deck_version_rejects_an_empty_file(app: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            f"/decks/{UUID_A}/versions",
            files={"file": ("d.txt", b"   ", "text/plain")},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_add_deck_version_404s_on_an_unknown_deck(app: FastAPI) -> None:
    with _svc("update_deck", new=AsyncMock(return_value=None)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.post(
                f"/decks/{UUID_A}/versions",
                files={"file": ("d.txt", b"1 Sol Ring\n", "text/plain")},
            )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_add_deck_version_404s_on_a_malformed_id_without_touching_the_db(
    app: FastAPI,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            "/decks/not-a-uuid/versions",
            files={"file": ("d.txt", b"1 Sol Ring\n", "text/plain")},
        )
    assert resp.status_code == 404
    app.state.read_engine.begin.assert_not_called()
