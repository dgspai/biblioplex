"""D13.3 — ``POST /decks/suggest`` (§6.9).

Delta-driven per-slot top-K. Reuses the /analyze pipeline for the delta
computation, then for each slot with delta < 0 issues a per-slot query
filtered by predicted-label membership, in-deck exclusion, and (when
supplied) commander color identity.

The fake here extends ``test_decks_analyze._FakeConn``'s shape with two
extra SQL routes:
* ``SELECT color_identity FROM cards WHERE LOWER(name) = LOWER(:name)``
  → the commander resolver.
* ``SELECT raw, predicted_labels, label_scores FROM cards WHERE :label =
  ANY(predicted_labels) ...`` → the per-slot suggestion query. The fake
  returns a canned list indexed by ``label`` and honors the ``:seen``
  exclusion.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex.classify.rules import LABELS, TARGETS
from biblioplex.config import Settings
from biblioplex.main import create_app
from biblioplex.routers.cards import get_read_engine
from biblioplex.routers.decks._common import DECKLIST_MAX_LEN

_SOL = UUID("11111111-1111-1111-1111-111111111111")
_BOLT = UUID("22222222-2222-2222-2222-222222222222")
_CULT = UUID("33333333-3333-3333-3333-333333333333")
_FOREST = UUID("44444444-4444-4444-4444-444444444444")
_KREN = UUID("55555555-5555-5555-5555-555555555555")

_RAMP_CANDIDATE = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_CA_CANDIDATE = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


class _Row:
    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)

    def one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


class _FakeConn:
    """SQL-routed fake covering fold + commander + per-slot suggestion queries."""

    def __init__(
        self,
        *,
        exact_rows: list[Any],
        slot_rows: list[Any],
        commander_row: Any = None,
        suggestions_by_label: dict[str, list[Any]] | None = None,
    ) -> None:
        self._exact = exact_rows
        self._slots = slot_rows
        self._commander_row = commander_row
        self._suggestions = suggestions_by_label or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _fold_rows(self, params: dict[str, Any]) -> list[Any]:
        names = list(params.get("names", []))
        lnames = list(params.get("lnames", []))
        exact_by_lname: dict[str, Any] = {r.lname: r.id for r in self._exact}
        slot_by_id: dict[Any, Any] = {r.id: r for r in self._slots}
        out: list[Any] = []
        for input_name, lname in zip(names, lnames, strict=True):
            hit_id = exact_by_lname.get(lname)
            if hit_id is None:
                continue
            slot = slot_by_id.get(hit_id)
            if slot is None:
                continue
            merged = _Row(
                input_name=input_name,
                id=hit_id,
                name=getattr(slot, "name", None),
                type_line=getattr(slot, "type_line", None),
                layout=getattr(slot, "layout", None),
                card_faces=getattr(slot, "card_faces", None),
                predicted_labels=getattr(slot, "predicted_labels", None),
                cmc=getattr(slot, "cmc", None),
                mana_cost=getattr(slot, "mana_cost", None),
                color_identity=getattr(slot, "color_identity", None),
            )
            out.append(merged)
        return out

    async def execute(self, stmt: Any, params: dict[str, Any]) -> _Result:
        sql = str(stmt)
        self.calls.append((sql, params))
        if "unnest(" in sql and ":ords" in sql:
            return _Result(self._fold_rows(params))
        if "color_identity FROM cards" in sql and ":name" in sql:
            return _Result([self._commander_row] if self._commander_row else [])
        if "unnest(CAST(:labels" in sql and "CROSS JOIN LATERAL" in sql:
            labels = list(params.get("labels", []))
            seen = set(params.get("seen", []))
            limit = params.get("limit", 5)
            ci_active = "color_identity <@ :ci" in sql
            ci_set = set(params.get("ci", [])) if ci_active else set()
            out: list[Any] = []
            for label in labels:
                candidates = list(self._suggestions.get(label, []))
                filtered = [
                    c for c in candidates if getattr(c, "id", None) not in seen
                ]
                if ci_active:
                    filtered = [
                        c for c in filtered
                        if set(getattr(c, "color_identity", []) or []).issubset(ci_set)
                    ]
                for cand in filtered[:limit]:
                    out.append(
                        _Row(
                            label=label,
                            raw=cand.raw,
                            predicted_labels=cand.predicted_labels,
                            label_scores=cand.label_scores,
                        )
                    )
            return _Result(out)
        raise AssertionError(f"unexpected SQL: {sql!r}")


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


async def _client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---- guard cases ----

async def test_empty_decklist_returns_422(app: FastAPI) -> None:
    conn = _FakeConn(exact_rows=[], slot_rows=[])
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        resp = await client.post("/decks/suggest", json={"decklist": ""})
    assert resp.status_code == 422
    assert resp.json()["code"] == "bad_request"


async def test_oversize_decklist_returns_422(app: FastAPI) -> None:
    conn = _FakeConn(exact_rows=[], slot_rows=[])
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    big = "1 Sol Ring\n" * (DECKLIST_MAX_LEN // 10)
    async for client in _client(app):
        resp = await client.post("/decks/suggest", json={"decklist": big})
    assert resp.status_code == 422


async def test_limit_out_of_range_returns_422(app: FastAPI) -> None:
    """FastAPI Query validator maps ge=1/le=20 to 422 automatically."""
    conn = _FakeConn(exact_rows=[], slot_rows=[])
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        resp = await client.post(
            "/decks/suggest?limit=100", json={"decklist": "1 Sol Ring"}
        )
    assert resp.status_code == 422


async def test_unknown_commander_returns_422(app: FastAPI) -> None:
    conn = _FakeConn(
        exact_rows=[_Row(id=_SOL, lname="sol ring")],
        slot_rows=[_Row(id=_SOL, name="Sol Ring", type_line="Artifact",
                        predicted_labels=["ramp"], cmc=1, mana_cost="{1}",
                        color_identity=[], layout="normal", card_faces=None)],
        commander_row=None,
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        resp = await client.post(
            "/decks/suggest",
            json={"decklist": "1 Sol Ring", "commander": "Nonesuch"},
        )
    assert resp.status_code == 422
    assert resp.json()["code"] == "commander_not_found"


# ---- happy path ----

async def test_underfilled_slots_return_suggestions(app: FastAPI) -> None:
    """One-ramp deck has ramp delta ~-11 → suggestions for ramp shipped;
    filled slots (there are none here — every slot underfilled) all appear.
    Assert that at least the `ramp` slot returns the canned candidate."""
    conn = _FakeConn(
        exact_rows=[_Row(id=_SOL, lname="sol ring")],
        slot_rows=[
            _Row(id=_SOL, name="Sol Ring", type_line="Artifact",
                 predicted_labels=["ramp"], cmc=1, mana_cost="{1}",
                 color_identity=[], layout="normal", card_faces=None),
        ],
        suggestions_by_label={
            "ramp": [
                _Row(
                    id=_RAMP_CANDIDATE,
                    raw={"id": str(_RAMP_CANDIDATE), "name": "Cultivate"},
                    predicted_labels=["ramp"],
                    label_scores={"ramp": 0.95},
                    color_identity=["G"],
                ),
            ],
        },
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        resp = await client.post(
            "/decks/suggest", json={"decklist": "1 Sol Ring"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "deck_suggestions"
    assert set(body["deltas"].keys()) == set(LABELS)
    assert body["deltas"]["ramp"] == 1 - TARGETS["ramp"]
    assert "ramp" in body["suggestions"]
    ramp_list = body["suggestions"]["ramp"]
    assert len(ramp_list) == 1
    assert ramp_list[0]["name"] == "Cultivate"
    # /suggest decorates with predicted_labels + label_scores like other endpoints.
    assert ramp_list[0]["predicted_labels"] == ["ramp"]
    assert ramp_list[0]["label_scores"] == {"ramp": 0.95}
    assert body["targets"] == dict(TARGETS)


async def test_filled_slots_absent_from_suggestions(app: FastAPI) -> None:
    """Slots with delta >= 0 don't appear in `suggestions`. Simulate a
    deck where `ramp` is over-filled by mocking 20 ramp cards at qty=1
    each (delta > 0)."""
    ramp_ids = [
        UUID(f"{i:08d}-0000-0000-0000-000000000000") for i in range(1, 21)
    ]
    exact_rows = [
        _Row(id=rid, lname=f"ramper {i}") for i, rid in enumerate(ramp_ids)
    ]
    slot_rows = [
        _Row(id=rid, name=f"Ramper {i}", type_line="Artifact",
             predicted_labels=["ramp"], cmc=1, mana_cost="{1}",
             color_identity=[], layout="normal", card_faces=None)
        for i, rid in enumerate(ramp_ids)
    ]
    decklist = "\n".join(f"1 Ramper {i}" for i in range(20))
    conn = _FakeConn(
        exact_rows=exact_rows,
        slot_rows=slot_rows,
        suggestions_by_label={"ramp": []},
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        resp = await client.post(
            "/decks/suggest", json={"decklist": decklist}
        )
    body = resp.json()
    assert body["deltas"]["ramp"] == 20 - TARGETS["ramp"]  # +8
    assert "ramp" not in body["suggestions"]


async def test_commander_constrains_suggestions_to_color_identity(app: FastAPI) -> None:
    """commander=Krenko (mono-R) → suggestions only include R-or-colorless cards."""
    conn = _FakeConn(
        exact_rows=[_Row(id=_BOLT, lname="lightning bolt")],
        slot_rows=[
            _Row(id=_BOLT, name="Lightning Bolt", type_line="Instant",
                 predicted_labels=["targeted_disrupt"], cmc=1, mana_cost="{R}",
                 color_identity=["R"], layout="normal", card_faces=None),
        ],
        commander_row=_Row(color_identity=["R"]),
        suggestions_by_label={
            "ramp": [
                _Row(
                    id=_RAMP_CANDIDATE,
                    raw={"id": str(_RAMP_CANDIDATE), "name": "Cultivate"},
                    predicted_labels=["ramp"],
                    label_scores={"ramp": 0.95},
                    color_identity=["G"],  # NOT R → must be filtered out.
                ),
                _Row(
                    id=_CA_CANDIDATE,
                    raw={"id": str(_CA_CANDIDATE), "name": "Sol Ring"},
                    predicted_labels=["ramp"],
                    label_scores={"ramp": 0.95},
                    color_identity=[],  # colorless → subset of R → keep.
                ),
            ],
        },
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        resp = await client.post(
            "/decks/suggest",
            json={"decklist": "1 Lightning Bolt", "commander": "Krenko"},
        )
    body = resp.json()
    assert body["commander_color_identity"] == ["R"]
    ramp_names = [c["name"] for c in body["suggestions"].get("ramp", [])]
    assert ramp_names == ["Sol Ring"]  # green Cultivate filtered out


async def test_in_deck_card_excluded_from_suggestions(app: FastAPI) -> None:
    """A card already in the resolved decklist must not appear in its slot."""
    conn = _FakeConn(
        exact_rows=[_Row(id=_SOL, lname="sol ring")],
        slot_rows=[
            _Row(id=_SOL, name="Sol Ring", type_line="Artifact",
                 predicted_labels=["ramp"], cmc=1, mana_cost="{1}",
                 color_identity=[], layout="normal", card_faces=None),
        ],
        suggestions_by_label={
            "ramp": [
                _Row(
                    id=_SOL,  # already in deck!
                    raw={"id": str(_SOL), "name": "Sol Ring"},
                    predicted_labels=["ramp"],
                    label_scores={"ramp": 0.95},
                    color_identity=[],
                ),
                _Row(
                    id=_RAMP_CANDIDATE,
                    raw={"id": str(_RAMP_CANDIDATE), "name": "Cultivate"},
                    predicted_labels=["ramp"],
                    label_scores={"ramp": 0.95},
                    color_identity=["G"],
                ),
            ],
        },
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        resp = await client.post(
            "/decks/suggest", json={"decklist": "1 Sol Ring"}
        )
    body = resp.json()
    ramp_names = [c["name"] for c in body["suggestions"]["ramp"]]
    assert "Sol Ring" not in ramp_names
    assert "Cultivate" in ramp_names


async def test_all_underfilled_slots_batched_in_one_query(app: FastAPI) -> None:
    """D15.7: N underfilled labels must resolve via ONE per-slot SQL call.

    Pre-fix code fired one query per label (up to 5 for a fresh deck).
    """
    conn = _FakeConn(
        exact_rows=[_Row(id=_SOL, lname="sol ring")],
        slot_rows=[
            _Row(id=_SOL, name="Sol Ring", type_line="Artifact",
                 predicted_labels=["ramp"], cmc=1, mana_cost="{1}",
                 color_identity=[], layout="normal", card_faces=None),
        ],
        # Every LABELS slot underfilled → prior code hit 5 SQLs.
        suggestions_by_label={
            lab: [_Row(id=UUID(int=i + 1),
                       raw={"id": str(UUID(int=i + 1)), "name": f"{lab}-{i}"},
                       predicted_labels=[lab],
                       label_scores={lab: 0.9},
                       color_identity=[])]
            for i, lab in enumerate(LABELS)
        },
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        resp = await client.post("/decks/suggest", json={"decklist": "1 Sol Ring"})
    assert resp.status_code == 200
    slot_calls = [
        (sql, params) for sql, params in conn.calls
        if "predicted_labels" in sql and ":ords" not in sql  # skip resolve fold
    ]
    assert len(slot_calls) == 1, f"expected 1 batched slot query, got {len(slot_calls)}"


async def test_limit_per_slot_capped(app: FastAPI) -> None:
    """limit=1 returns exactly one card per underfilled slot."""
    conn = _FakeConn(
        exact_rows=[_Row(id=_SOL, lname="sol ring")],
        slot_rows=[
            _Row(id=_SOL, name="Sol Ring", type_line="Artifact",
                 predicted_labels=["ramp"], cmc=1, mana_cost="{1}",
                 color_identity=[], layout="normal", card_faces=None),
        ],
        suggestions_by_label={
            "ramp": [
                _Row(id=UUID(int=i), raw={"id": str(UUID(int=i)), "name": f"R{i}"},
                     predicted_labels=["ramp"], label_scores={"ramp": 0.9},
                     color_identity=[])
                for i in range(1, 6)
            ],
        },
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        resp = await client.post(
            "/decks/suggest?limit=1", json={"decklist": "1 Sol Ring"}
        )
    body = resp.json()
    assert len(body["suggestions"]["ramp"]) == 1
