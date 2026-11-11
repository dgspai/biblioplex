"""D12.4 — `POST /decks/analyze` (§6.7)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from biblioplex.classify.rules import TARGETS
from biblioplex.config import Settings
from biblioplex.main import create_app
from biblioplex.routers.cards import get_read_engine
from biblioplex.routers.decks._common import DECKLIST_MAX_LEN

_SOL = UUID("11111111-1111-1111-1111-111111111111")
_BOLT = UUID("22222222-2222-2222-2222-222222222222")
_CULTIVATE = UUID("33333333-3333-3333-3333-333333333333")
_FOREST = UUID("44444444-4444-4444-4444-444444444444")


class _Row:
    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("cmc", None)
        kwargs.setdefault("mana_cost", None)
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
    """SQL-substring-routed fake conn used by the router pipeline.

    Post-D13.4 the analyze router issues a SINGLE fold query using
    ``unnest(:ords::int[], :names::text[], :lnames::text[])`` — this
    fake detects the fold via the ``unnest(:ords::int[]`` substring and
    synthesizes the joined-row output from ``exact_rows`` (lname → id
    map) + ``slot_rows`` (id → slot columns). Missed inputs pop from
    ``fuzzy_results`` in order, same shape as pre-D13.4.
    """

    def __init__(
        self,
        exact_rows: list[Any],
        slot_rows: list[Any],
        fuzzy_results: list[list[Any]] | None = None,
    ) -> None:
        self._exact = exact_rows
        self._slots = slot_rows
        self._fuzzy = fuzzy_results or []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _fold_rows(self, params: dict[str, Any]) -> list[Any]:
        names = list(params.get("names", []))
        lnames = list(params.get("lnames", []))
        exact_by_lname: dict[str, Any] = {
            r.lname: r.id for r in self._exact
        }
        slot_by_id: dict[Any, Any] = {r.id: r for r in self._slots}
        out: list[Any] = []
        for input_name, lname in zip(names, lnames, strict=True):
            hit_id = exact_by_lname.get(lname)
            if hit_id is None and self._fuzzy:
                nxt = self._fuzzy.pop(0)
                if nxt:
                    hit_id = getattr(nxt[0], "id", None)
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


async def test_empty_decklist_returns_422(app: FastAPI) -> None:
    conn = _FakeConn(exact_rows=[], slot_rows=[])
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        resp = await client.post("/decks/analyze", json={"decklist": ""})
    assert resp.status_code == 422
    body = resp.json()
    assert body["object"] == "error"
    assert body["code"] == "bad_request"


async def test_oversize_decklist_returns_422(app: FastAPI) -> None:
    conn = _FakeConn(exact_rows=[], slot_rows=[])
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    big = "1 Sol Ring\n" * (DECKLIST_MAX_LEN // 10)
    async for client in _client(app):
        resp = await client.post("/decks/analyze", json={"decklist": big})
    assert resp.status_code == 422
    assert "too long" in resp.json()["details"]


async def test_happy_path_with_one_ramp_card(app: FastAPI) -> None:
    """Single Sol Ring → counts.ramp == qty, deltas reflect target gap."""
    conn = _FakeConn(
        exact_rows=[_Row(id=_SOL, lname="sol ring")],
        slot_rows=[
            _Row(
                id=_SOL,
                name="Sol Ring",
                type_line="Artifact",
                predicted_labels=["ramp"],
            ),
        ],
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)

    async for client in _client(app):
        resp = await client.post(
            "/decks/analyze",
            json={"decklist": "1 Sol Ring"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "deck_analysis"
    assert body["counts"]["ramp"] == 1
    assert body["counts"]["land"] == 0
    assert body["targets"] == dict(TARGETS)
    assert body["deltas"]["ramp"] == 1 - TARGETS["ramp"]
    assert body["unresolved"] == []
    assert body["cards"] == [
        {
            "id": str(_SOL),
            "name": "Sol Ring",
            "qty": 1,
            "predicted_labels": ["ramp"],
            "is_land": False,
        },
    ]


async def test_ramp_tagged_land_flagged_is_land(app: FastAPI) -> None:
    """A land tagged "ramp" counts as land, not ramp — and cards[] carries
    is_land=True so a client slot drill-down can exclude it (Stage 8 review)."""
    conn = _FakeConn(
        exact_rows=[_Row(id=_SOL, lname="ancient tomb")],
        slot_rows=[
            _Row(
                id=_SOL,
                name="Ancient Tomb",
                type_line="Land",
                predicted_labels=["ramp"],
            ),
        ],
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)

    async for client in _client(app):
        resp = await client.post("/decks/analyze", json={"decklist": "1 Ancient Tomb"})

    body = resp.json()
    assert body["counts"]["ramp"] == 0
    assert body["counts"]["land"] == 1
    assert body["cards"] == [
        {
            "id": str(_SOL),
            "name": "Ancient Tomb",
            "qty": 1,
            "predicted_labels": ["ramp"],
            "is_land": True,
        },
    ]


async def test_curve_and_pips_populated_for_nonland(app: FastAPI) -> None:
    """Non-land rows contribute their bucket + colored pips scaled by qty."""
    conn = _FakeConn(
        exact_rows=[_Row(id=_SOL, lname="sol ring")],
        slot_rows=[
            _Row(
                id=_SOL,
                name="Sol Ring",
                type_line="Artifact",
                predicted_labels=["ramp"],
                cmc=1,
                mana_cost="{1}",
            ),
        ],
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        resp = await client.post(
            "/decks/analyze",
            json={"decklist": "2 Sol Ring"},
        )
    body = resp.json()
    assert body["curve"]["1"] == 2
    assert body["curve"]["0"] == 0
    assert body["pips"] == {"W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0}


async def test_curve_caps_at_seven_plus_and_pips_count_hybrids(app: FastAPI) -> None:
    """CMC >= 7 stacks in the `7+` bucket; hybrid `{U/B}` adds to both colors."""
    conn = _FakeConn(
        exact_rows=[_Row(id=_SOL, lname="hybrid haymaker")],
        slot_rows=[
            _Row(
                id=_SOL,
                name="Hybrid Haymaker",
                type_line="Sorcery",
                predicted_labels=["plan"],
                cmc=9,
                mana_cost="{U/B}{U/B}{W}",
            ),
        ],
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        resp = await client.post(
            "/decks/analyze",
            json={"decklist": "1 Hybrid Haymaker"},
        )
    body = resp.json()
    assert body["curve"]["7+"] == 1
    assert body["pips"]["U"] == 2
    assert body["pips"]["B"] == 2
    assert body["pips"]["W"] == 1


async def test_d14_20_curve_covers_cmc_0_through_9_bucketed_correctly(
    app: FastAPI,
) -> None:
    """D14.20 acceptance: seeded rows across CMC 0..9 land in the right bucket.

    CMC 0..6 → own bucket; CMC 7..9 → "7+" (cap). Also pins:
    - land row NOT counted in curve (only in counts.land);
    - X-cost card (Scryfall stores cmc=0) lands in "0" bucket.
    """
    ids = [UUID(f"{i:08x}-0000-0000-0000-000000000000") for i in range(11)]
    # Ten spells (cmc 0..9) + one land + one X-cost spell (cmc=0).
    exact = [
        _Row(id=ids[i], lname=f"card{i}") for i in range(10)
    ] + [
        _Row(id=ids[10], lname="basic land"),
        _Row(id=UUID("aaaaaaaa-0000-0000-0000-000000000000"), lname="fireball"),
    ]
    slots = [
        _Row(
            id=ids[i],
            name=f"Card{i}",
            type_line="Sorcery",
            predicted_labels=[],
            cmc=i,
            mana_cost=f"{{{i}}}",
        )
        for i in range(10)
    ] + [
        _Row(
            id=ids[10],
            name="Basic Land",
            type_line="Basic Land — Forest",
            predicted_labels=[],
            cmc=0,
            mana_cost=None,
        ),
        # X-cost: Scryfall stores cmc=0 (the constant part). Verify it
        # lands in the "0" bucket, not misrouted / dropped.
        _Row(
            id=UUID("aaaaaaaa-0000-0000-0000-000000000000"),
            name="Fireball",
            type_line="Sorcery",
            predicted_labels=[],
            cmc=0,
            mana_cost="{X}{R}",
        ),
    ]
    conn = _FakeConn(exact_rows=exact, slot_rows=slots)
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    decklist = "\n".join(
        [f"1 Card{i}" for i in range(10)] + ["1 Basic Land", "1 Fireball"]
    )
    async for client in _client(app):
        resp = await client.post("/decks/analyze", json={"decklist": decklist})
    body = resp.json()
    # X-cost "Fireball" (cmc=0) + spell Card0 (cmc=0) → 2 in the "0" bucket.
    assert body["curve"]["0"] == 2
    for i in range(1, 7):
        assert body["curve"][str(i)] == 1, f"cmc={i}"
    # CMC 7..9 all fold into "7+".
    assert body["curve"]["7+"] == 3
    # Land NOT in curve; counted separately.
    assert body["counts"]["land"] == 1
    total_curve = sum(body["curve"].values())
    # 10 non-land spells + Fireball = 11 (Basic Land excluded).
    assert total_curve == 11


async def test_curve_targets_and_deltas_follow_guide_bell(app: FastAPI) -> None:
    """`curve_targets` is the guide §6 bell; `curve_deltas` = actual - target.

    MV6+ folds curve buckets "6" and "7+" into a single "6+" key (the
    guide slots MV6+ as one bucket of 5). MV0 has no target (not in the
    bell) and does not appear in either new dict.
    """
    ids = [UUID(f"{i:08x}-0000-0000-0000-000000000000") for i in range(10)]
    exact = [_Row(id=ids[i], lname=f"card{i}") for i in range(10)]
    slots = [
        _Row(
            id=ids[i],
            name=f"Card{i}",
            type_line="Sorcery",
            predicted_labels=[],
            cmc=i,
            mana_cost=f"{{{i}}}",
        )
        for i in range(10)
    ]
    conn = _FakeConn(exact_rows=exact, slot_rows=slots)
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    decklist = "\n".join(f"1 Card{i}" for i in range(10))
    async for client in _client(app):
        resp = await client.post("/decks/analyze", json={"decklist": decklist})
    body = resp.json()
    assert body["curve_targets"] == {
        "1": 9,
        "2": 18,
        "3": 15,
        "4": 10,
        "5": 5,
        "6+": 5,
    }
    # Actual: MV1..5 = 1 each; MV6+ = cmc6 (1) + cmc7,8,9 (3) = 4. MV0 absent.
    assert body["curve_deltas"] == {
        "1": 1 - 9,
        "2": 1 - 18,
        "3": 1 - 15,
        "4": 1 - 10,
        "5": 1 - 5,
        "6+": 4 - 5,
    }
    assert "0" not in body["curve_targets"]


async def test_multilabel_card_counts_in_every_slot(app: FastAPI) -> None:
    """A `[ramp, card_advantage]` card adds qty to BOTH slots."""
    conn = _FakeConn(
        exact_rows=[_Row(id=_SOL, lname="dual hat")],
        slot_rows=[
            _Row(
                id=_SOL,
                name="Dual Hat",
                type_line="Artifact",
                predicted_labels=["ramp", "card_advantage"],
            ),
        ],
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        resp = await client.post(
            "/decks/analyze",
            json={"decklist": "2 Dual Hat"},
        )
    body = resp.json()
    assert body["counts"]["ramp"] == 2
    assert body["counts"]["card_advantage"] == 2


async def test_land_in_counts_not_in_deltas(app: FastAPI) -> None:
    """Basic lands populate counts.land; deltas only spans LABELS."""
    conn = _FakeConn(
        exact_rows=[_Row(id=_FOREST, lname="forest")],
        slot_rows=[
            _Row(
                id=_FOREST,
                name="Forest",
                type_line="Basic Land — Forest",
                predicted_labels=[],
            ),
        ],
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        resp = await client.post(
            "/decks/analyze",
            json={"decklist": "10 Forest"},
        )
    body = resp.json()
    assert body["counts"]["land"] == 10
    assert "land" not in body["deltas"]
    assert "land" not in body["targets"]


async def test_type_line_with_land_substring_is_not_land(app: FastAPI) -> None:
    """Word-boundary land check: a subtype containing 'land' as substring
    (e.g. a hypothetical `Creature — Woodlander`) must NOT bucket as land."""
    conn = _FakeConn(
        exact_rows=[_Row(id=_SOL, lname="woodlander")],
        slot_rows=[
            _Row(
                id=_SOL,
                name="Woodlander",
                type_line="Creature — Human Woodlander",
                predicted_labels=["plan"],
                cmc=2,
                mana_cost="{1}{G}",
            ),
        ],
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        resp = await client.post(
            "/decks/analyze",
            json={"decklist": "1 Woodlander"},
        )
    body = resp.json()
    assert body["counts"]["land"] == 0
    assert body["counts"]["plan"] == 1
    assert body["curve"]["2"] == 1


async def test_mdfc_back_land_counted_as_spell(app: FastAPI) -> None:
    """MDFC with land back (Bala Ged Recovery, Emeria's Call) counts as a
    spell — its CMC hits `curve`, its pips hit `pips`, and it does NOT
    inflate `counts.land` (D13.2)."""
    conn = _FakeConn(
        exact_rows=[_Row(id=_SOL, lname="bala ged recovery")],
        slot_rows=[
            _Row(
                id=_SOL,
                name="Bala Ged Recovery // Bala Ged Sanctuary",
                type_line="Sorcery // Land — Desert",
                layout="modal_dfc",
                card_faces=[
                    {"type_line": "Sorcery", "name": "Bala Ged Recovery"},
                    {"type_line": "Land — Desert", "name": "Bala Ged Sanctuary"},
                ],
                predicted_labels=["plan"],
                cmc=3,
                mana_cost="{2}{G}",
            ),
        ],
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        resp = await client.post(
            "/decks/analyze",
            json={"decklist": "1 Bala Ged Recovery"},
        )
    body = resp.json()
    assert body["counts"]["land"] == 0
    assert body["counts"]["plan"] == 1
    assert body["curve"]["3"] == 1
    assert body["pips"]["G"] == 1


async def test_mdfc_land_front_face_still_counts_as_land(app: FastAPI) -> None:
    """Symmetric: if a hypothetical MDFC has a LAND front face, still a land."""
    conn = _FakeConn(
        exact_rows=[_Row(id=_SOL, lname="landy front")],
        slot_rows=[
            _Row(
                id=_SOL,
                name="Landy Front // Something Back",
                type_line="Land — Forest // Sorcery",
                layout="modal_dfc",
                card_faces=[
                    {"type_line": "Land — Forest", "name": "Landy Front"},
                    {"type_line": "Sorcery", "name": "Something Back"},
                ],
                predicted_labels=[],
                cmc=0,
                mana_cost="",
            ),
        ],
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        resp = await client.post(
            "/decks/analyze",
            json={"decklist": "1 Landy Front"},
        )
    body = resp.json()
    assert body["counts"]["land"] == 1


async def test_transform_back_land_counted_as_spell(app: FastAPI) -> None:
    """`transform` layout with a land back (Search for Azcanta shape) —
    front face is enchantment, counts as spell not land."""
    conn = _FakeConn(
        exact_rows=[_Row(id=_SOL, lname="search for azcanta")],
        slot_rows=[
            _Row(
                id=_SOL,
                name="Search for Azcanta // Azcanta, the Sunken Ruin",
                type_line="Legendary Enchantment // Legendary Land",
                layout="transform",
                card_faces=[
                    {"type_line": "Legendary Enchantment", "name": "Search for Azcanta"},
                    {"type_line": "Legendary Land", "name": "Azcanta"},
                ],
                predicted_labels=["card_advantage"],
                cmc=1,
                mana_cost="{U}",
            ),
        ],
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        resp = await client.post(
            "/decks/analyze",
            json={"decklist": "1 Search for Azcanta"},
        )
    body = resp.json()
    assert body["counts"]["land"] == 0
    assert body["counts"]["card_advantage"] == 1
    assert body["curve"]["1"] == 1
    assert body["pips"]["U"] == 1


async def test_split_card_with_type_line_slash_not_land(app: FastAPI) -> None:
    """Split card (Fire // Ice) — layout != MDFC, no `land` in either half.
    Regression that non-MDFC slash-joined type_lines still work."""
    conn = _FakeConn(
        exact_rows=[_Row(id=_SOL, lname="fire // ice")],
        slot_rows=[
            _Row(
                id=_SOL,
                name="Fire // Ice",
                type_line="Instant // Instant",
                layout="split",
                card_faces=[
                    {"type_line": "Instant", "name": "Fire"},
                    {"type_line": "Instant", "name": "Ice"},
                ],
                predicted_labels=["targeted_disrupt"],
                cmc=2,
                mana_cost="{1}{R}",
            ),
        ],
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        resp = await client.post(
            "/decks/analyze",
            json={"decklist": "1 Fire // Ice"},
        )
    body = resp.json()
    assert body["counts"]["land"] == 0
    assert body["counts"]["targeted_disrupt"] == 1


async def test_unresolved_input_listed(app: FastAPI) -> None:
    """No exact, no fuzzy → input flows into unresolved."""
    conn = _FakeConn(
        exact_rows=[],
        slot_rows=[],
        fuzzy_results=[[]],  # single miss → fuzzy fallback returns empty
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    async for client in _client(app):
        resp = await client.post(
            "/decks/analyze",
            json={"decklist": "1 Zxqyzz"},
        )
    body = resp.json()
    assert body["unresolved"] == ["Zxqyzz"]
    assert body["cards"] == []
    assert body["counts"]["ramp"] == 0


async def test_mixed_decklist_tally(app: FastAPI) -> None:
    """Realistic mini-deck: ramp + bolt + land. Counts sum correctly."""
    conn = _FakeConn(
        exact_rows=[
            _Row(id=_SOL, lname="sol ring"),
            _Row(id=_CULTIVATE, lname="cultivate"),
            _Row(id=_BOLT, lname="lightning bolt"),
            _Row(id=_FOREST, lname="forest"),
        ],
        slot_rows=[
            _Row(
                id=_SOL,
                name="Sol Ring",
                type_line="Artifact",
                predicted_labels=["ramp"],
            ),
            _Row(
                id=_CULTIVATE,
                name="Cultivate",
                type_line="Sorcery",
                predicted_labels=["ramp"],
            ),
            _Row(
                id=_BOLT,
                name="Lightning Bolt",
                type_line="Instant",
                predicted_labels=["targeted_disrupt"],
            ),
            _Row(
                id=_FOREST,
                name="Forest",
                type_line="Basic Land — Forest",
                predicted_labels=[],
            ),
        ],
    )
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine(conn)
    decklist = (
        "1 Sol Ring\n"
        "1 Cultivate\n"
        "4 Lightning Bolt\n"
        "8 Forest\n"
    )
    async for client in _client(app):
        resp = await client.post("/decks/analyze", json={"decklist": decklist})
    body = resp.json()
    assert body["counts"]["ramp"] == 2
    assert body["counts"]["targeted_disrupt"] == 4
    assert body["counts"]["land"] == 8
    # Card listing preserves input order.
    assert [c["name"] for c in body["cards"]] == [
        "Sol Ring",
        "Cultivate",
        "Lightning Bolt",
        "Forest",
    ]
    assert [c["qty"] for c in body["cards"]] == [1, 1, 4, 8]
