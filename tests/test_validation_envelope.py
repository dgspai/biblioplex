"""FastAPI request-validation 422s use the same Scryfall envelope as other errors."""

from __future__ import annotations

from biblioplex.main import create_app

from ._search_helpers import client, settings


async def test_bad_path_param_is_scryfall_envelope() -> None:
    app = create_app(settings=settings())
    async for c in client(app):
        resp = await c.get("/cards/not-a-uuid")
    assert resp.status_code == 422
    body = resp.json()
    assert "detail" not in body
    assert body["object"] == "error"
    assert body["status"] == 422
    assert body["code"] == "bad_request"
    # Field info is kept: human string + structured per-field list.
    assert "path.card_id" in body["details"]
    assert body["warnings"][0].startswith("path.card_id: ")


async def test_missing_body_field_names_the_field() -> None:
    app = create_app(settings=settings())
    async for c in client(app):
        resp = await c.post("/decks/analyze", json={})
    assert resp.status_code == 422
    body = resp.json()
    assert (body["object"], body["status"], body["code"]) == ("error", 422, "bad_request")
    assert any(w.startswith("body.") for w in body["warnings"])
