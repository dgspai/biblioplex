"""Format-legality checks for a decklist (tech_spec.md §6.11, D14.7).

Pure module — the router side (``routers/decks.py:decklist_legality``)
resolves names + fetches ``legalities`` / ``color_identity`` / ``type_line``
via the shared ``resolve_and_fetch_deck_rows`` fold and hands them here.

Rules by format
---------------

**Commander**
    * Deck size == 100 (99 + commander when supplied; otherwise the
      whole 100 must be provided in the decklist body).
    * Singleton non-basic. Basic Lands (``Basic`` in ``type_line``) are
      exempt. Curated "any number of copies" catalog (Relentless Rats,
      Rat Colony, …) is exempt too.
    * ``legalities['commander']`` must equal ``'legal'``.
    * Every card's ``color_identity`` must be a subset of the
      commander's when supplied. Skipped when ``commander_ci`` is ``None``.

**Constructed** (modern / legacy / pioneer / standard / vintage / …)
    * Deck size >= 60.
    * Max 4 copies of any non-basic. Basic Lands exempt.
    * ``legalities[fmt]`` must equal ``'legal'`` or ``'restricted'``.
      Restricted implies max 1 copy (vintage-specific).
    * Color identity check is skipped — CI is a commander-format concept.

ponytail: rules-as-data. Anything beyond commander + constructed
(brawl, oathbreaker, pauper edge cases) becomes another entry in
``_FORMAT_PROFILES`` when someone needs it. "Any number of copies"
exemption is data-driven via ``cards.has_unlimited_copies`` (D15.24) —
kills the drift class of bug where new "any-number" cards ship
without a code update.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "COMMANDER_FORMATS",
    "LegalityResult",
    "Violation",
    "check_legality",
]


# Formats that follow the singleton + color-identity + 100-card rules.
# Commander is the only widely-played case; brawl is close enough that
# it lives here too when someone wires ``fmt="brawl"``.
COMMANDER_FORMATS: frozenset[str] = frozenset({"commander", "brawl"})


@dataclass(frozen=True)
class Violation:
    """One rule violation surfaced from ``check_legality``.

    ``reason`` slugs are stable — clients pattern-match on them. Current
    set: ``not_legal``, ``over_max_copies``, ``color_identity``,
    ``deck_size``, ``unresolved``.
    """

    card: str
    reason: str
    detail: str


@dataclass(frozen=True)
class LegalityResult:
    """Composed verdict from ``check_legality``.

    ``legal`` is ``True`` iff ``violations`` is empty. ``deck_size`` is
    the total card count summed from ``qty_by_lower`` (excluding the
    commander itself — the caller adds 1 for the commander when
    computing ``expected_deck_size``).
    """

    legal: bool
    format: str
    deck_size: int
    expected_deck_size: int
    commander_color_identity: list[str] | None
    violations: list[Violation] = field(default_factory=list)


def _is_basic_land(row: Any) -> bool:
    """Basic Land exemption via ``type_line`` substring.

    Catches every basic + snow-covered variant + Wastes in one check —
    all of them have ``Basic`` in ``type_line`` per Scryfall.
    """
    tl = getattr(row, "type_line", None) or ""
    return "Basic" in tl


def _fmt_status(row: Any, fmt: str) -> str:
    """Read ``legalities[fmt]``; missing key = ``'not_legal'``."""
    legalities = getattr(row, "legalities", None) or {}
    return str(legalities.get(fmt, "not_legal"))


def _has_unlimited_copies(row: Any) -> bool:
    """Data-driven "any number of copies" exemption (D15.24).

    Reads the ``has_unlimited_copies`` boolean generated column on
    ``cards``. Defaults to ``False`` when the attribute is absent so
    unit-test rows built without the flag stay conservative (cap
    enforced) — the router-fed rows always carry it in production.
    """
    return bool(getattr(row, "has_unlimited_copies", False))


def _commander_max_copies(row: Any) -> int:
    if _is_basic_land(row) or _has_unlimited_copies(row):
        return 99  # effectively unlimited within a 100-card deck
    return 1


def _constructed_max_copies(row: Any, fmt_status: str) -> int:
    if _is_basic_land(row) or _has_unlimited_copies(row):
        return 99
    if fmt_status == "restricted":
        return 1
    return 4


def check_legality(
    *,
    qty_by_lower: dict[str, int],
    rows_by_lower: dict[str, Any],
    fmt: str,
    commander_ci: list[str] | None,
    unresolved: list[str],
) -> LegalityResult:
    """Compute the legality verdict for a resolved decklist.

    Args:
        qty_by_lower: Deck body — lowered name → copy count. The
            commander's copy is NOT included here (the caller resolves
            it separately and passes its CI through ``commander_ci``).
        rows_by_lower: Lowered name → resolved row (must expose
            ``name``, ``color_identity``, ``legalities``, ``type_line``).
        fmt: Format slug matching ``legalities`` keys (``commander``,
            ``modern``, ``legacy``, ``pioneer``, ``standard``, ``vintage``,
            …). Unknown slug is treated as "every card fails" via the
            missing-key branch of ``_fmt_status``.
        commander_ci: Commander's color identity when the caller resolved
            one. ``None`` for a decklist without a supplied commander
            (either constructed format or a commander deck without an
            explicit body param).
        unresolved: Names that missed both exact + fuzzy resolve passes.
            Surfaced as ``reason='unresolved'`` violations — non-blocking
            in spirit but block the ``legal`` flag until fixed.

    Returns:
        :class:`LegalityResult` with ``legal = not violations``.
    """
    violations: list[Violation] = []
    is_commander = fmt in COMMANDER_FORMATS

    deck_size = sum(qty_by_lower.values())
    expected_deck_size = 100 if is_commander else 60

    # Deck-size check
    if is_commander:
        # Commander body should be 99 cards + 1 commander = 100. If the
        # commander was supplied via body param, the caller passed 99
        # here; if it wasn't (agent submitted the whole 100 inline), we
        # accept 100 too.
        expected_body = 99 if commander_ci is not None else 100
        if deck_size != expected_body:
            violations.append(
                Violation(
                    card="<deck>",
                    reason="deck_size",
                    detail=(
                        f"{deck_size} cards in body, expected {expected_body} "
                        "(commander deck must be exactly 100 including commander)"
                    ),
                )
            )
    else:
        if deck_size < 60:
            violations.append(
                Violation(
                    card="<deck>",
                    reason="deck_size",
                    detail=f"{deck_size} cards, expected at least 60",
                )
            )

    # Unresolved surface up front so users see them even if the rest of
    # the deck is otherwise clean.
    for name in unresolved:
        violations.append(
            Violation(
                card=name,
                reason="unresolved",
                detail="name did not match any card in the corpus",
            )
        )

    for lname, qty in qty_by_lower.items():
        row = rows_by_lower.get(lname)
        if row is None:
            continue  # already surfaced via `unresolved`
        display_name = getattr(row, "name", lname)

        # Format legality
        status = _fmt_status(row, fmt)
        if status not in ("legal", "restricted"):
            violations.append(
                Violation(
                    card=display_name,
                    reason="not_legal",
                    detail=f"legalities.{fmt} = {status!r}",
                )
            )
        elif status == "restricted" and is_commander:
            # Restricted only applies to constructed (currently Vintage);
            # in commander this shouldn't appear, but surface it anyway.
            violations.append(
                Violation(
                    card=display_name,
                    reason="not_legal",
                    detail=f"legalities.{fmt} = 'restricted' (unexpected in {fmt})",
                )
            )

        # Copies
        if is_commander:
            max_copies = _commander_max_copies(row)
        else:
            max_copies = _constructed_max_copies(row, status)
        if qty > max_copies:
            violations.append(
                Violation(
                    card=display_name,
                    reason="over_max_copies",
                    detail=f"{qty} copies, max {max_copies}",
                )
            )

        # Color identity (commander only)
        if is_commander and commander_ci is not None:
            card_ci = list(getattr(row, "color_identity", None) or [])
            allowed = set(commander_ci)
            extras = [c for c in card_ci if c not in allowed]
            if extras:
                violations.append(
                    Violation(
                        card=display_name,
                        reason="color_identity",
                        detail=(
                            f"card CI {card_ci} not subset of commander CI "
                            f"{list(commander_ci)}"
                        ),
                    )
                )

    return LegalityResult(
        legal=not violations,
        format=fmt,
        deck_size=deck_size,
        expected_deck_size=expected_deck_size,
        commander_color_identity=list(commander_ci) if commander_ci else None,
        violations=violations,
    )
