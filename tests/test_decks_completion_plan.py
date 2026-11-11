"""D19.45 — `GET /decks/completion-plan`.

The objective is DECKS COMPLETED within a budget, not cards-per-dollar, so
these pin a PLAN rather than an ordering. The sharp edges are all in the
greedy loop: marginal (not independent) costs, a cart shared across decks,
and the unpriced split that is the reason two plans exist at all.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from biblioplex.decks import service as deck_service
from biblioplex.routers import deck_library


def _rows(*rows: tuple[str, str, str, str, int, float | None]) -> MagicMock:
    mapped = [
        {
            "deck_id": d,
            "deck_name": dn,
            "oracle_id": o,
            "card_name": cn,
            "missing": m,
            "price_usd": p,
        }
        for d, dn, o, cn, m, p in rows
    ]
    mappings = MagicMock()
    mappings.all.return_value = mapped
    res = MagicMock()
    res.mappings.return_value = mappings
    conn = AsyncMock()
    conn.execute.return_value = res
    return conn


# Deck A: 1 cheap card. Deck B: 3 cards but cheaper in total than A's one.
# Deck C: duplicate of B — must complete for nothing once B is bought.
FIXTURE = (
    ("A", "Alpha", "o1", "Big Card", 1, 10.0),
    ("B", "Beta", "o2", "Cheap One", 1, 1.0),
    ("B", "Beta", "o3", "Cheap Two", 1, 1.0),
    ("B", "Beta", "o4", "Cheap Three", 1, 1.0),
    ("C", "Gamma", "o2", "Cheap One", 1, 1.0),
    ("C", "Gamma", "o3", "Cheap Two", 1, 1.0),
    ("C", "Gamma", "o4", "Cheap Three", 1, 1.0),
)


@pytest.mark.asyncio
async def test_by_cost_and_by_cards_disagree() -> None:
    """The whole reason both are returned.

    Beta is cheaper ($3 vs $10) but costs three cards; Alpha is one card.
    A single ranking would hide that the caller has a real choice.
    """
    plan = await deck_service.completion_plan(_rows(*FIXTURE))
    assert plan["by_cost"]["steps"][0]["deck_name"] == "Beta"
    assert plan["by_cards"]["steps"][0]["deck_name"] == "Alpha"


@pytest.mark.asyncio
async def test_cost_is_marginal_so_a_duplicate_deck_is_free() -> None:
    """Gamma shares Beta's whole gap: once Beta is bought, Gamma costs 0.

    Summing per-deck `missing_usd` would report 3 + 3 = 6 for the pair.
    """
    plan = await deck_service.completion_plan(_rows(*FIXTURE))
    by_cost = plan["by_cost"]["steps"]
    gamma = next(s for s in by_cost if s["deck_name"] == "Gamma")
    assert gamma["marginal_usd"] == 0.0
    assert gamma["marginal_cards"] == 0
    assert plan["by_cost"]["spend_usd"] == 13.0, "6 would mean costs were independent"


@pytest.mark.asyncio
async def test_budget_is_respected_and_stops_the_plan() -> None:
    plan = await deck_service.completion_plan(_rows(*FIXTURE), budget=5)
    assert plan["by_cost"]["spend_usd"] <= 5
    assert "Alpha" not in [s["deck_name"] for s in plan["by_cost"]["steps"]]
    # Beta ($3) plus its free duplicate still fit.
    assert plan["by_cost"]["decks_completed"] == 2


@pytest.mark.asyncio
async def test_max_cards_is_respected() -> None:
    plan = await deck_service.completion_plan(_rows(*FIXTURE), max_cards=1)
    assert plan["by_cards"]["cards_to_buy"] <= 1
    assert plan["by_cards"]["steps"][0]["deck_name"] == "Alpha"


@pytest.mark.asyncio
async def test_budget_and_max_cards_compose() -> None:
    plan = await deck_service.completion_plan(_rows(*FIXTURE), budget=5, max_cards=2)
    assert plan["by_cost"]["spend_usd"] <= 5
    assert plan["by_cost"]["cards_to_buy"] <= 2


# ---- the unpriced split: why there are two plans ----

UNPRICED = (
    ("A", "Alpha", "o1", "Priced Card", 1, 2.0),
    ("A", "Alpha", "o9", "No Price Here", 1, None),
    ("B", "Beta", "o2", "Cheap One", 1, 1.0),
    ("B", "Beta", "o3", "Cheap Two", 1, 1.0),
)


@pytest.mark.asyncio
async def test_by_cost_skips_unpriced_decks_but_reports_them() -> None:
    """Silently dropping them is how the cheapest-looking deck vanishes."""
    plan = await deck_service.completion_plan(_rows(*UNPRICED))
    assert "Alpha" not in [s["deck_name"] for s in plan["by_cost"]["steps"]]
    skipped = plan["by_cost"]["skipped_unpriced"]
    assert [s["deck_name"] for s in skipped] == ["Alpha"]
    assert skipped[0]["unpriced_cards"] == ["No Price Here"]


@pytest.mark.asyncio
async def test_by_cards_can_rank_an_unpriced_deck() -> None:
    """Card counts need no prices — this is what rescues those decks."""
    plan = await deck_service.completion_plan(_rows(*UNPRICED))
    names = [s["deck_name"] for s in plan["by_cards"]["steps"]]
    assert "Alpha" in names


@pytest.mark.asyncio
async def test_by_cards_marks_its_total_a_floor() -> None:
    """A partial sum presented as exact is the trap `missing_usd` sets."""
    plan = await deck_service.completion_plan(_rows(*UNPRICED))
    assert plan["by_cards"]["spend_usd_is_floor"] is True
    assert plan["by_cost"]["spend_usd_is_floor"] is False
    step = next(s for s in plan["by_cards"]["steps"] if s["deck_name"] == "Alpha")
    assert step["cost_is_partial"] is True


@pytest.mark.asyncio
async def test_unresolved_rows_are_excluded_by_the_query() -> None:
    """They can never match inventory and have no price, so they cannot
    be planned for. Excluding them in SQL keeps the loop honest."""
    conn = _rows(*FIXTURE)
    await deck_service.completion_plan(conn)
    sql = str(conn.execute.await_args.args[0])
    assert "dc.oracle_id IS NOT NULL" in sql
    assert "GREATEST(dc.qty - COALESCE(inv.owned, 0), 0) > 0" in sql


@pytest.mark.asyncio
async def test_ownership_folds_printings_with_sum() -> None:
    """`inventory` is one row per PRINTING (D18.1); a row read keeps one."""
    conn = _rows(*FIXTURE)
    await deck_service.completion_plan(conn)
    sql = str(conn.execute.await_args.args[0])
    assert "SUM(quantity)" in sql and "GROUP BY oracle_id" in sql


@pytest.mark.asyncio
async def test_plan_is_deterministic_across_identical_calls() -> None:
    """Ties need a second AND third key or the plan wobbles."""
    a = await deck_service.completion_plan(_rows(*FIXTURE))
    b = await deck_service.completion_plan(_rows(*FIXTURE))
    assert [s["deck_name"] for s in a["by_cost"]["steps"]] == [
        s["deck_name"] for s in b["by_cost"]["steps"]
    ]


@pytest.mark.asyncio
async def test_response_admits_greedy_is_not_optimal() -> None:
    """A caller told the answer is optimal stops looking."""
    plan = await deck_service.completion_plan(_rows(*FIXTURE))
    assert "not proven optimal" in plan["note"].lower()


@pytest.mark.asyncio
async def test_empty_library_is_not_an_error() -> None:
    plan = await deck_service.completion_plan(_rows())
    assert plan["incomplete_decks"] == 0
    assert plan["by_cost"]["decks_completed"] == 0
    assert plan["by_cost"]["spend_usd"] == 0.0
    assert plan["by_cards"]["steps"] == []


# ---- routing ----


def test_route_is_declared_before_the_deck_id_catch_all() -> None:
    """D19.16's trap: a literal sub-path after `/{deck_id}` is parsed as an id."""
    paths = [getattr(r, "path", "") for r in deck_library.router.routes]
    plan = next(p for p in paths if p.endswith("/completion-plan"))
    catch_all = next(p for p in paths if p.endswith("/{deck_id}"))
    assert paths.index(plan) < paths.index(catch_all)


def test_route_uses_a_read_connection() -> None:
    src = deck_library.__dict__["get_deck_completion_plan"].__doc__
    assert src is not None
    import inspect

    body = inspect.getsource(deck_library.get_deck_completion_plan)
    assert "engine.connect()" in body
    assert "engine.begin()" not in body, "a plan reads; it must never open a write txn"
