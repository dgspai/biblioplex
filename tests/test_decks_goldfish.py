"""D14.17 — ``POST /decks/goldfish`` deterministic solitaire sim (§6.20)."""

from __future__ import annotations

import random

import pytest

from biblioplex.decklist.goldfish import run_goldfish


def _pool(spells_cmc: list[int], lands: int) -> list[tuple[str, int | None, bool]]:
    """Convenience — N lands + spells at the listed cmcs."""
    p: list[tuple[str, int | None, bool]] = [
        ("Forest", None, True) for _ in range(lands)
    ]
    p.extend((f"Spell{c}", c, False) for c in spells_cmc)
    return p


# ---- pure fn ----


def test_run_goldfish_zero_stats_on_empty_pool() -> None:
    stats = run_goldfish(
        [], sims=100, turns=6, hand_size=7, rng=random.Random(42)
    )
    assert stats.sims == 0
    assert stats.avg_mana_by_turn == ()
    assert stats.hands_kept == 0


def test_run_goldfish_zero_stats_when_hand_larger_than_pool() -> None:
    stats = run_goldfish(
        _pool([1, 2], 3),  # 5-card pool
        sims=10,
        turns=6,
        hand_size=7,
        rng=random.Random(42),
    )
    assert stats.sims == 0


def test_run_goldfish_deterministic_under_seed() -> None:
    pool = _pool([1, 2, 3, 4, 5], 40) * 4  # replicate for size
    stats_a = run_goldfish(
        pool, sims=50, turns=6, hand_size=7, rng=random.Random(7)
    )
    stats_b = run_goldfish(
        pool, sims=50, turns=6, hand_size=7, rng=random.Random(7)
    )
    assert stats_a.avg_mana_by_turn == stats_b.avg_mana_by_turn
    assert stats_a.avg_spells_cast_by_turn == stats_b.avg_spells_cast_by_turn
    assert stats_a.sample_lines == stats_b.sample_lines


def test_run_goldfish_averages_length_match_turns() -> None:
    pool = _pool([1, 2, 3], 40) * 4
    stats = run_goldfish(
        pool, sims=20, turns=6, hand_size=7, rng=random.Random(1)
    )
    assert len(stats.avg_mana_by_turn) == 6
    assert len(stats.avg_spells_cast_by_turn) == 6
    assert len(stats.avg_lands_in_play_by_turn) == 6


def test_run_goldfish_lands_monotonic_across_turns() -> None:
    # A pure fn invariant: avg lands in play should be non-decreasing
    # turn over turn (you don't LOSE lands under the sim rules).
    pool = _pool([1, 2, 3], 40) * 4
    stats = run_goldfish(
        pool, sims=200, turns=6, hand_size=7, rng=random.Random(3)
    )
    for i in range(1, len(stats.avg_lands_in_play_by_turn)):
        assert (
            stats.avg_lands_in_play_by_turn[i]
            >= stats.avg_lands_in_play_by_turn[i - 1]
        )


def test_run_goldfish_sample_lines_one_per_turn() -> None:
    pool = _pool([1, 2, 3], 40) * 4
    stats = run_goldfish(
        pool, sims=5, turns=6, hand_size=7, rng=random.Random(5)
    )
    assert len(stats.sample_lines) == 6
    # Each line starts with "T<n>:"
    for i, line in enumerate(stats.sample_lines, start=1):
        assert line.startswith(f"T{i}:")


# ---- endpoint smoke via TestClient ----


from collections.abc import AsyncIterator  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import UUID  # noqa: E402

from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from biblioplex.decklist.resolve import ResolvedDeckRow  # noqa: E402
from biblioplex.main import create_app  # noqa: E402
from biblioplex.routers.cards import get_read_engine  # noqa: E402
from biblioplex.routers.decks._common import DECKLIST_MAX_LEN  # noqa: E402

from .test_decks_analyze import _settings  # noqa: E402


class _FakeEngine:
    def __init__(self) -> None:
        pass

    def connect(self) -> _FakeEngine:
        return self

    async def __aenter__(self) -> _FakeEngine:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _row(name: str, cmc: float, is_land: bool) -> SimpleNamespace:
    return SimpleNamespace(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        name=name,
        type_line="Land" if is_land else "Sorcery",
        layout="normal",
        card_faces=None,
        predicted_labels=[],
        cmc=cmc,
        mana_cost="{1}",
        color_identity=[],
        legalities={},
        has_unlimited_copies=False,
        price_usd=None,
    )


async def _override(app: FastAPI, monkeypatch: pytest.MonkeyPatch, entries_shape: list[tuple[str, float, bool]]) -> None:
    resolved = [
        ResolvedDeckRow(input_name=name, row=_row(name, cmc, is_land))
        for (name, cmc, is_land) in entries_shape
    ]

    async def _fake_resolve(conn: object, names: list[str]) -> tuple:
        return resolved, []

    import biblioplex.routers.decks.goldfish as decks_mod
    app.dependency_overrides[get_read_engine] = lambda: _FakeEngine()
    monkeypatch.setattr(decks_mod, "resolve_and_fetch_deck_rows", _fake_resolve)


@pytest.fixture
def app() -> FastAPI:
    return create_app(settings=_settings())


async def _client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_goldfish_endpoint_envelope(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    # 40 lands + 60 spells at various cmcs → enough for a 7-card opening
    # and 6 turns of draws.
    shape = [("Forest", 0.0, True)] * 40 + [
        (f"Spell{c}", float(c), False) for c in ([1] * 20 + [2] * 20 + [3] * 20)
    ]
    # Distinct names required — build unique ones.
    shape = (
        [(f"Forest{i}", 0.0, True) for i in range(40)]
        + [(f"OneDrop{i}", 1.0, False) for i in range(20)]
        + [(f"TwoDrop{i}", 2.0, False) for i in range(20)]
        + [(f"ThreeDrop{i}", 3.0, False) for i in range(20)]
    )
    await _override(app, monkeypatch, shape)
    decklist = "\n".join(f"1 {name}" for name, _c, _il in shape)
    async for client in _client(app):
        resp = await client.post(
            "/decks/goldfish?turns=6",
            json={"decklist": decklist, "hands": 20, "seed": 1},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "deck_goldfish"
    assert body["turns"] == 6
    assert len(body["avg_mana_by_turn"]) == 6
    assert len(body["sample_lines"]) == 6


async def test_goldfish_empty_body_rejected(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    async for client in _client(app):
        resp = await client.post("/decks/goldfish", json={"decklist": ""})
    assert resp.status_code == 422


async def test_goldfish_oversize_rejected(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    async for client in _client(app):
        resp = await client.post(
            "/decks/goldfish", json={"decklist": "1 X\n" * (DECKLIST_MAX_LEN // 3)}
        )
    assert resp.status_code == 422
