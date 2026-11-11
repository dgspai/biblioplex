"""Pure-fn tests for ``decklist/legality.py`` (D14.7)."""

from __future__ import annotations

from typing import Any

from biblioplex.decklist.legality import Violation, check_legality


class _Row:
    """Duck-typed resolver row exposing name, color_identity, legalities."""

    def __init__(
        self,
        *,
        name: str,
        color_identity: list[str] | None = None,
        legalities: dict[str, str] | None = None,
        type_line: str = "",
        has_unlimited_copies: bool = False,
    ) -> None:
        self.name = name
        self.color_identity = color_identity or []
        self.legalities = legalities or {}
        self.type_line = type_line
        self.has_unlimited_copies = has_unlimited_copies


def _resolved(**rows: _Row) -> dict[str, _Row]:
    """Build a name-lower → row map matching the resolver's post-condition."""
    return {name.lower(): row for name, row in rows.items()}


# ---- Commander format ----


def test_commander_legal_deck_with_100_cards() -> None:
    """99 singleton non-basic + 1 basic (36 copies) = 99 body + 1 commander.

    Convention: when the commander is supplied via body param, the router
    resolves it separately and passes its CI through ``commander_ci`` —
    the commander itself is NOT in ``qty_by_lower``.
    """
    qty: dict[str, int] = {"sol ring": 1, "plains": 36}
    for i in range(62):
        qty[f"filler_{i}"] = 1
    rows: dict[str, Any] = {
        "sol ring": _Row(
            name="Sol Ring",
            color_identity=[],
            legalities={"commander": "legal"},
        ),
        "plains": _Row(
            name="Plains",
            color_identity=["W"],
            legalities={"commander": "legal"},
            type_line="Basic Land — Plains",
        ),
    }
    for i in range(62):
        rows[f"filler_{i}"] = _Row(
            name=f"Filler {i}",
            color_identity=["W"],
            legalities={"commander": "legal"},
        )
    result = check_legality(
        qty_by_lower=qty,
        rows_by_lower=rows,
        fmt="commander",
        commander_ci=["W", "U", "B", "G"],
        unresolved=[],
    )
    assert result.legal
    assert result.violations == []
    assert result.deck_size == 99
    assert result.expected_deck_size == 100


def test_commander_singleton_violation() -> None:
    """Non-basic card with qty>1 in commander → over_max_copies."""
    qty = {"sol ring": 2}
    rows = _resolved(
        **{
            "Sol Ring": _Row(
                name="Sol Ring",
                legalities={"commander": "legal"},
            ),
        }
    )
    result = check_legality(
        qty_by_lower=qty,
        rows_by_lower=rows,
        fmt="commander",
        commander_ci=None,
        unresolved=[],
    )
    assert not result.legal
    assert any(v.reason == "over_max_copies" for v in result.violations)


def test_commander_basic_land_exempt_from_singleton() -> None:
    """Basic Land in type_line → any number of copies allowed."""
    qty = {"plains": 36}
    rows = _resolved(
        **{
            "Plains": _Row(
                name="Plains",
                legalities={"commander": "legal"},
                type_line="Basic Land — Plains",
            ),
        }
    )
    result = check_legality(
        qty_by_lower=qty,
        rows_by_lower=rows,
        fmt="commander",
        commander_ci=None,
        unresolved=[],
    )
    assert all(v.reason != "over_max_copies" for v in result.violations)


def test_commander_banned_card_surfaces_violation() -> None:
    """`legalities[commander] == 'banned'` → not_legal violation."""
    qty = {"black lotus": 1}
    rows = _resolved(
        **{
            "Black Lotus": _Row(
                name="Black Lotus",
                legalities={"commander": "banned"},
            ),
        }
    )
    result = check_legality(
        qty_by_lower=qty,
        rows_by_lower=rows,
        fmt="commander",
        commander_ci=None,
        unresolved=[],
    )
    assert not result.legal
    v = next(v for v in result.violations if v.card == "Black Lotus")
    assert v.reason == "not_legal"


def test_commander_color_identity_violation() -> None:
    """Card with CI outside commander's CI → color_identity violation."""
    qty = {"lightning bolt": 1}
    rows = _resolved(
        **{
            "Lightning Bolt": _Row(
                name="Lightning Bolt",
                color_identity=["R"],
                legalities={"commander": "legal"},
            ),
        }
    )
    result = check_legality(
        qty_by_lower=qty,
        rows_by_lower=rows,
        fmt="commander",
        commander_ci=["W", "U"],
        unresolved=[],
    )
    assert not result.legal
    assert any(v.reason == "color_identity" for v in result.violations)


def test_commander_ci_none_skips_ci_check() -> None:
    """No commander supplied → CI check disabled; other checks still run."""
    qty = {"lightning bolt": 1}
    rows = _resolved(
        **{
            "Lightning Bolt": _Row(
                name="Lightning Bolt",
                color_identity=["R"],
                legalities={"commander": "legal"},
            ),
        }
    )
    result = check_legality(
        qty_by_lower=qty,
        rows_by_lower=rows,
        fmt="commander",
        commander_ci=None,
        unresolved=[],
    )
    assert all(v.reason != "color_identity" for v in result.violations)


def test_d15_24_unlimited_copies_flag_exempts_from_commander_singleton() -> None:
    """D15.24: has_unlimited_copies=True bypasses the singleton cap."""
    qty = {"rat colony": 40}
    rows = _resolved(
        **{
            "Rat Colony": _Row(
                name="Rat Colony",
                color_identity=["B"],
                legalities={"commander": "legal"},
                has_unlimited_copies=True,
            ),
        }
    )
    result = check_legality(
        qty_by_lower=qty,
        rows_by_lower=rows,
        fmt="commander",
        commander_ci=None,
        unresolved=[],
    )
    # No over_max_copies violation despite 40 copies.
    assert all(v.reason != "over_max_copies" for v in result.violations)


def test_d15_24_unlimited_copies_flag_exempts_from_constructed_max_4() -> None:
    """D15.24: has_unlimited_copies=True bypasses the max-4 rule in constructed."""
    qty = {"dragon's approach": 40}
    # 60-card floor met by padding with other cards
    for i in range(20):
        qty[f"filler_{i}"] = 1
    rows: dict[str, Any] = {
        "dragon's approach": _Row(
            name="Dragon's Approach",
            legalities={"modern": "legal"},
            has_unlimited_copies=True,
        ),
    }
    for i in range(20):
        rows[f"filler_{i}"] = _Row(
            name=f"Filler {i}",
            legalities={"modern": "legal"},
        )
    result = check_legality(
        qty_by_lower=qty,
        rows_by_lower=rows,
        fmt="modern",
        commander_ci=None,
        unresolved=[],
    )
    assert all(v.reason != "over_max_copies" for v in result.violations)


def test_d15_24_no_flag_still_enforces_max_copies() -> None:
    """Cards without the flag still hit the 4-copy cap."""
    qty = {"lightning bolt": 5}
    for i in range(60):
        qty[f"filler_{i}"] = 1
    rows: dict[str, Any] = {
        "lightning bolt": _Row(
            name="Lightning Bolt",
            legalities={"modern": "legal"},
            has_unlimited_copies=False,
        ),
    }
    for i in range(60):
        rows[f"filler_{i}"] = _Row(
            name=f"Filler {i}",
            legalities={"modern": "legal"},
        )
    result = check_legality(
        qty_by_lower=qty,
        rows_by_lower=rows,
        fmt="modern",
        commander_ci=None,
        unresolved=[],
    )
    assert any(
        v.reason == "over_max_copies" and v.card == "Lightning Bolt"
        for v in result.violations
    )


def test_commander_wrong_deck_size() -> None:
    """Non-100 deck size (with commander) → deck_size violation."""
    qty = {"sol ring": 1}
    rows = _resolved(
        **{
            "Sol Ring": _Row(
                name="Sol Ring",
                legalities={"commander": "legal"},
            ),
        }
    )
    result = check_legality(
        qty_by_lower=qty,
        rows_by_lower=rows,
        fmt="commander",
        commander_ci=[],
        unresolved=[],
    )
    assert any(v.reason == "deck_size" for v in result.violations)
    assert result.deck_size == 1
    assert result.expected_deck_size == 100


# ---- Constructed (modern) format ----


def test_modern_max_4_copies_ok() -> None:
    """Non-basic card at qty=4 in modern → no over_max_copies violation."""
    qty = {"lightning bolt": 4}
    rows = _resolved(
        **{
            "Lightning Bolt": _Row(
                name="Lightning Bolt",
                legalities={"modern": "legal"},
            ),
        }
    )
    for i in range(56):
        qty[f"filler_{i}"] = 1
        rows[f"filler_{i}"] = _Row(
            name=f"Filler {i}",
            legalities={"modern": "legal"},
        )
    result = check_legality(
        qty_by_lower=qty,
        rows_by_lower=rows,
        fmt="modern",
        commander_ci=None,
        unresolved=[],
    )
    assert all(v.reason != "over_max_copies" for v in result.violations)


def test_modern_max_4_copies_exceeded() -> None:
    """qty=5 of a non-basic → over_max_copies."""
    qty = {"lightning bolt": 5}
    rows = _resolved(
        **{
            "Lightning Bolt": _Row(
                name="Lightning Bolt",
                legalities={"modern": "legal"},
            ),
        }
    )
    result = check_legality(
        qty_by_lower=qty,
        rows_by_lower=rows,
        fmt="modern",
        commander_ci=None,
        unresolved=[],
    )
    assert any(v.reason == "over_max_copies" for v in result.violations)


def test_modern_min_deck_size_60() -> None:
    """Deck under 60 cards → deck_size violation for constructed formats."""
    qty = {"lightning bolt": 4}
    rows = _resolved(
        **{
            "Lightning Bolt": _Row(
                name="Lightning Bolt",
                legalities={"modern": "legal"},
            ),
        }
    )
    result = check_legality(
        qty_by_lower=qty,
        rows_by_lower=rows,
        fmt="modern",
        commander_ci=None,
        unresolved=[],
    )
    v = next((v for v in result.violations if v.reason == "deck_size"), None)
    assert v is not None
    assert result.expected_deck_size == 60


def test_modern_ci_check_skipped() -> None:
    """CI enforcement is a commander-only rule."""
    qty = {"lightning bolt": 4}
    rows = _resolved(
        **{
            "Lightning Bolt": _Row(
                name="Lightning Bolt",
                color_identity=["R"],
                legalities={"modern": "legal"},
            ),
        }
    )
    for i in range(56):
        qty[f"filler_{i}"] = 1
        rows[f"filler_{i}"] = _Row(
            name=f"Filler {i}",
            color_identity=["W"],
            legalities={"modern": "legal"},
        )
    result = check_legality(
        qty_by_lower=qty,
        rows_by_lower=rows,
        fmt="modern",
        commander_ci=["W"],  # ignored for non-commander formats
        unresolved=[],
    )
    assert all(v.reason != "color_identity" for v in result.violations)


def test_missing_legality_key_treated_as_not_legal() -> None:
    """Format not in `legalities` map → not_legal violation."""
    qty = {"chaos orb": 1}
    rows = _resolved(
        **{
            "Chaos Orb": _Row(
                name="Chaos Orb",
                legalities={},  # no keys at all
            ),
        }
    )
    result = check_legality(
        qty_by_lower=qty,
        rows_by_lower=rows,
        fmt="modern",
        commander_ci=None,
        unresolved=[],
    )
    assert any(v.reason == "not_legal" for v in result.violations)


def test_unresolved_names_surface_as_violations() -> None:
    """Names that missed both resolve passes surface as `unresolved` reason."""
    qty = {"lightning bolt": 4}
    rows: dict[str, Any] = {}
    result = check_legality(
        qty_by_lower=qty,
        rows_by_lower=rows,
        fmt="modern",
        commander_ci=None,
        unresolved=["Lightning Bolt"],
    )
    assert any(v.reason == "unresolved" for v in result.violations)


def test_violation_dataclass_fields() -> None:
    """Violation carries card, reason, detail."""
    v = Violation(card="X", reason="not_legal", detail="banned")
    assert v.card == "X"
    assert v.reason == "not_legal"
    assert v.detail == "banned"
