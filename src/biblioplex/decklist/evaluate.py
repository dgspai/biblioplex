"""Deck-evaluation tips engine — pure function over a ``/decks/analyze``
payload.

Turns the numeric analysis (slot deltas, curve deltas, land count,
ownership) into a human summary plus a list of guide-driven improvement
tips (``docs/edh-guide.md``). Deterministic and DB-free so it is trivial
to test and reusable by both ``GET /decks/:id/evaluate`` and the builder
wizard's turn-7 step.

Each tip is ``{code, severity, message}``. ``code`` is a stable slug for
clients to pattern-match / sort on; ``severity`` is ``warn`` (a template
gap the guide treats as non-negotiable — lands and the four functional
slots) or ``info`` (curve shaping, plan headroom, ownership).
"""

from __future__ import annotations

from typing import Any

# The land mandate and the human labels for the four functional slots
# whose under-target is a hard warning. ``plan`` is the "meat" — light on
# plan is worth noting but never a warning, so it sits out of this map.
_LAND_TARGET = 38
_COMMANDER_DECK_SIZE = 100
_SLOT_LABELS: dict[str, str] = {
    "ramp": "ramp",
    "card_advantage": "card advantage",
    "targeted_disrupt": "targeted disruption",
    "mass_disrupt": "mass disruption",
}


def _curve_peak(curve: dict[str, int]) -> str | None:
    """Bucket carrying the most nonland spells; ties break to lower MV.

    ``None`` when the deck has no nonland cards yet (an empty build).
    """
    order = ("0", "1", "2", "3", "4", "5", "6", "7+")  # ascending MV
    best: str | None = None
    best_n = -1
    for bucket in order:
        n = curve.get(bucket, 0)
        if n > best_n:
            best_n, best = n, bucket
    return best if best_n > 0 else None


def evaluate_deck(analysis: dict[str, Any]) -> dict[str, Any]:
    """Summarise + grade an analyze payload against the guide template."""
    counts: dict[str, int] = analysis.get("counts", {})
    targets: dict[str, int] = analysis.get("targets", {})
    deltas: dict[str, int] = analysis.get("deltas", {})
    curve: dict[str, int] = analysis.get("curve", {})
    curve_deltas: dict[str, int] = analysis.get("curve_deltas", {})
    inv = analysis.get("inventory_summary") or {}

    cards: list[dict[str, Any]] = analysis.get("cards", [])

    lands = counts.get("land", 0)
    nonland = sum(curve.values())
    total = nonland + lands
    coverage_pct = inv.get("coverage_pct")
    peak = _curve_peak(curve)
    # Overlap — cards serving 2+ functional slots (§3 "serve multiple
    # masters"), qty-weighted.
    overlap = sum(
        c.get("qty", 1) for c in cards if len(c.get("predicted_labels") or []) >= 2
    )

    summary = {
        "total_cards": total,
        "lands": lands,
        "nonland": nonland,
        "curve_peak": peak,
        "coverage_pct": coverage_pct,
        "overlap": overlap,
        "slots": {
            lab: {
                "count": counts.get(lab, 0),
                "target": tgt,
                "delta": deltas.get(lab, counts.get(lab, 0) - tgt),
            }
            for lab, tgt in targets.items()
        },
    }

    tips: list[dict[str, Any]] = []

    # Deck size — a Commander deck is 100 (commander counted in the body,
    # D19.10). Off-100 is a real build error, not a shaping note.
    if total and total != _COMMANDER_DECK_SIZE:
        tips.append(
            {
                "code": "wrong_size",
                "severity": "warn",
                "message": (
                    f"Deck has {total} cards; a Commander deck is "
                    f"{_COMMANDER_DECK_SIZE} (commander included)."
                ),
            }
        )

    # Lands — the 38 mandate (§4). Under is a consistency warning; a
    # small flood over is an info nudge.
    if lands < _LAND_TARGET:
        tips.append(
            {
                "code": "low_lands",
                "severity": "warn",
                "message": (
                    f"Only {lands} lands — the guide mandates "
                    f"{_LAND_TARGET} for early-game consistency."
                ),
            }
        )
    elif lands > _LAND_TARGET + 3:
        tips.append(
            {
                "code": "high_lands",
                "severity": "info",
                "message": (
                    f"{lands} lands is above the {_LAND_TARGET} baseline — "
                    "trim toward 38 unless the curve runs high."
                ),
            }
        )

    # Functional slots — under-target is a warning (§4 vegetables).
    for lab, human in _SLOT_LABELS.items():
        delta = deltas.get(lab, 0)
        if delta < 0:
            tips.append(
                {
                    "code": f"under_{lab}",
                    "severity": "warn",
                    "message": (
                        f"Add {-delta} more {human}: "
                        f"{counts.get(lab, 0)}/{targets.get(lab, 0)}."
                    ),
                }
            )

    # Plan (the meat) — light is worth noting, never a warning.
    if deltas.get("plan", 0) < 0:
        tips.append(
            {
                "code": "light_plan",
                "severity": "info",
                "message": (
                    f"Plan is light: {counts.get('plan', 0)}/"
                    f"{targets.get('plan', 0)} synergy/payoff pieces."
                ),
            }
        )

    # Curve shaping (§6). Peak should sit at MV2; MV5 over its cap is the
    # guide's "5-drop allergy".
    if peak is not None and peak != "2":
        tips.append(
            {
                "code": "curve_off_peak",
                "severity": "info",
                "message": (
                    f"Curve peaks at MV{peak}; the guide wants the peak "
                    "at MV2."
                ),
            }
        )
    if curve_deltas.get("5", 0) > 0:
        tips.append(
            {
                "code": "five_drop_allergy",
                "severity": "info",
                "message": (
                    f"{curve.get('5', 0)} five-drops — the guide flags a "
                    "'5-drop allergy'; a 5 must win the turn it lands."
                ),
            }
        )

    # Overlap — cards pulling double duty are the guide's prize (§3).
    if overlap > 0:
        tips.append(
            {
                "code": "overlap",
                "severity": "info",
                "message": (
                    f"{overlap} card{'s' if overlap != 1 else ''} serve 2+ "
                    "roles (overlap) — the guide prizes cards that serve "
                    "multiple masters."
                ),
            }
        )

    # Ownership — only when an inventory is synced.
    if coverage_pct is not None and coverage_pct < 100:
        tips.append(
            {
                "code": "partial_ownership",
                "severity": "info",
                "message": (
                    f"You own {coverage_pct:.0f}% of this deck; "
                    "see the missing cards in the card list below."
                ),
            }
        )

    return {"object": "deck_evaluation", "summary": summary, "tips": tips}
