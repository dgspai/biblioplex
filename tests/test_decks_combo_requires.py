"""Deck combo endpoints check Spellbook template requirements."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex.routers.cards import get_read_engine
from biblioplex.routers.decks.bracket import _COMBOS_COUNT_SQL
from tests import test_decks_combos as tc
from tests import test_decks_near_combos as tn

_PERSIST = {
    "name": "Persist Creature", "query": "keyword:persist t:creature", "quantity": 1,
    "checked": True, "in_deck": [], "missing": 1, "owned": 2,
}


@pytest.fixture
def app() -> FastAPI:
    return tc.create_app(settings=tc._settings())


@pytest.mark.asyncio
async def test_deck_combos_need_their_template_cards(app: FastAPI) -> None:
    done = {**_PERSIST, "in_deck": ["Kitchen Finks"], "missing": 0, "owned": None}
    conn = tc._FakeConn(
        resolve_rows=[tc._resolve_row("A", "aaaaaaaa-0000-0000-0000-000000000001")],
        combo_rows=[tc._combo_row(combo_id="c1", card_names=["A"], requires=[done])],
    )
    app.dependency_overrides[get_read_engine] = lambda: tc._FakeEngine(conn)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post("/decks/combos", json={"decklist": "1 A"})
    assert r.json()["combos"][0]["requires"] == [{
        "name": "Persist Creature", "query": "keyword:persist t:creature", "quantity": 1,
        "checked": True, "in_deck": ["Kitchen Finks"], "missing": 0,
    }]
    sql = next(s for s in conn.executed if "FROM combos" in s)
    assert "combo_requires" in sql and "req.missing = 0" in sql


def test_bracket_counts_only_combos_with_their_template_cards() -> None:
    assert "combo_requires" in _COMBOS_COUNT_SQL
    assert "req.missing = 0" in _COMBOS_COUNT_SQL


@pytest.mark.asyncio
async def test_near_combos_count_missing_template_cards(app: FastAPI) -> None:
    row = tn._near_combo_row(
        combo_id="c1", missing_count=1, gap_names=[], gap_ids=[], in_names=["A", "B"],
        requires=[_PERSIST],
    )
    r, sql, _ = await tn._near_call(app, "/decks/near-combos", [row])
    combo = r.json()["combos"][0]
    assert combo["gap"] == []
    assert combo["missing"] == 1
    assert combo["requires"][0]["owned"] == 2
    assert "req.missing" in sql and "req.all_owned" in sql
