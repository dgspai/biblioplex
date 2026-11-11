"""Unit tests for the pure deck-evaluation tips engine.

``evaluate_deck`` takes a ``/decks/analyze`` payload and derives a
summary + guide-driven improvement tips (edh-guide.md). Pure function,
no DB — the HTTP wiring at ``GET /decks/:id/evaluate`` is tested
separately once it composes the analyze pipeline.
"""

from __future__ import annotations

from biblioplex.decklist.evaluate import evaluate_deck


def _analysis(
    *,
    counts: dict[str, int],
    curve: dict[str, int],
    coverage_pct: float | None = None,
    cards: list[dict] | None = None,
) -> dict:
    """Build a minimal analyze-shaped payload for the engine."""
    targets = {
        "ramp": 12,
        "card_advantage": 12,
        "targeted_disrupt": 12,
        "mass_disrupt": 6,
        "plan": 30,
    }
    deltas = {lab: counts.get(lab, 0) - t for lab, t in targets.items()}
    curve_targets = {"1": 9, "2": 18, "3": 15, "4": 10, "5": 5, "6+": 5}
    a6 = curve.get("6", 0) + curve.get("7+", 0)
    curve_deltas = {
        k: (a6 if k == "6+" else curve.get(k, 0)) - t
        for k, t in curve_targets.items()
    }
    return {
        "object": "deck_analysis",
        "counts": counts,
        "targets": targets,
        "deltas": deltas,
        "curve": curve,
        "curve_targets": curve_targets,
        "curve_deltas": curve_deltas,
        "pips": {},
        "cards": cards or [],
        "unresolved": [],
        "inventory_summary": (
            {"coverage_pct": coverage_pct} if coverage_pct is not None else None
        ),
        "missing_cards": [],
    }


def _codes(result: dict) -> set[str]:
    return {t["code"] for t in result["tips"]}


def test_healthy_template_deck_has_no_slot_or_land_tips() -> None:
    """A deck that hits every target and 38 lands raises no warnings."""
    counts = {
        "ramp": 12,
        "card_advantage": 12,
        "targeted_disrupt": 12,
        "mass_disrupt": 6,
        "plan": 30,
        "land": 38,
    }
    # 62 nonland spells laid out exactly on the bell, peak at MV2.
    curve = {"0": 0, "1": 9, "2": 18, "3": 15, "4": 10, "5": 5, "6": 3, "7+": 2}
    res = evaluate_deck(_analysis(counts=counts, curve=curve))
    assert res["object"] == "deck_evaluation"
    assert res["summary"]["total_cards"] == 100
    assert res["summary"]["lands"] == 38
    assert res["summary"]["curve_peak"] == "2"
    # No under-target, no bad land count, peak already at 2.
    warn_codes = {t["code"] for t in res["tips"] if t["severity"] == "warn"}
    assert warn_codes == set()


def test_underfilled_slots_and_low_lands_produce_tips() -> None:
    counts = {
        "ramp": 2,
        "card_advantage": 0,
        "targeted_disrupt": 1,
        "mass_disrupt": 0,
        "plan": 30,
        "land": 33,
    }
    curve = {"0": 0, "1": 1, "2": 2, "3": 0, "4": 0, "5": 0, "6": 0, "7+": 0}
    res = evaluate_deck(_analysis(counts=counts, curve=curve))
    codes = _codes(res)
    assert "low_lands" in codes
    assert "under_ramp" in codes
    assert "under_card_advantage" in codes
    assert "under_targeted_disrupt" in codes
    assert "under_mass_disrupt" in codes
    # The ramp tip names the concrete gap (need 10 more: 2 -> 12).
    ramp_tip = next(t for t in res["tips"] if t["code"] == "under_ramp")
    assert "10" in ramp_tip["message"]


def test_five_drop_allergy_and_off_peak_curve() -> None:
    counts = {
        "ramp": 12,
        "card_advantage": 12,
        "targeted_disrupt": 12,
        "mass_disrupt": 6,
        "plan": 30,
        "land": 38,
    }
    # Peak sits at MV5, and MV5 is over its guide cap of 5.
    curve = {"0": 0, "1": 2, "2": 3, "3": 4, "4": 5, "5": 20, "6": 3, "7+": 2}
    res = evaluate_deck(_analysis(counts=counts, curve=curve))
    codes = _codes(res)
    assert "five_drop_allergy" in codes
    assert "curve_off_peak" in codes
    assert res["summary"]["curve_peak"] == "5"


def test_overlap_counts_multilabel_cards_qty_weighted() -> None:
    counts = {
        "ramp": 12,
        "card_advantage": 12,
        "targeted_disrupt": 12,
        "mass_disrupt": 6,
        "plan": 30,
        "land": 38,
    }
    curve = {"0": 0, "1": 9, "2": 18, "3": 15, "4": 10, "5": 5, "6": 3, "7+": 2}
    cards = [
        {"id": "1", "name": "Dual Hat", "qty": 2, "predicted_labels": ["ramp", "card_advantage"]},
        {"id": "2", "name": "Single", "qty": 1, "predicted_labels": ["plan"]},
        {"id": "3", "name": "Bare", "qty": 1, "predicted_labels": []},
    ]
    res = evaluate_deck(_analysis(counts=counts, curve=curve, cards=cards))
    assert res["summary"]["overlap"] == 2  # only the dual, qty-weighted
    assert "overlap" in _codes(res)


def test_no_overlap_tip_when_no_multilabel() -> None:
    counts = {
        "ramp": 12, "card_advantage": 12, "targeted_disrupt": 12,
        "mass_disrupt": 6, "plan": 30, "land": 38,
    }
    curve = {"0": 0, "1": 9, "2": 18, "3": 15, "4": 10, "5": 5, "6": 3, "7+": 2}
    cards = [{"id": "1", "name": "Single", "qty": 1, "predicted_labels": ["ramp"]}]
    res = evaluate_deck(_analysis(counts=counts, curve=curve, cards=cards))
    assert res["summary"]["overlap"] == 0
    assert "overlap" not in _codes(res)


def test_coverage_tip_when_inventory_partial() -> None:
    counts = {
        "ramp": 12,
        "card_advantage": 12,
        "targeted_disrupt": 12,
        "mass_disrupt": 6,
        "plan": 30,
        "land": 38,
    }
    curve = {"0": 0, "1": 9, "2": 18, "3": 15, "4": 10, "5": 5, "6": 3, "7+": 2}
    res = evaluate_deck(_analysis(counts=counts, curve=curve, coverage_pct=72.0))
    assert "partial_ownership" in _codes(res)
    assert res["summary"]["coverage_pct"] == 72.0
