"""Extract Portuguese printed names from Scryfall ``all_cards`` rows.

Pure projection + the SQL the ``scripts/build_pt_names.py`` job runs.
Kept out of the script file so the extraction rule is unit-testable
without a network stream or a DB.

A pt printing carries its Portuguese spelling in top-level
``printed_name`` for single-faced cards and in each
``card_faces[i].printed_name`` for multi-faced ones. Every spelling maps
to the printing's single top-level ``oracle_id``. English printings
(``lang != "pt"``) contribute nothing — those names resolve against
``cards.name`` directly.
"""

from __future__ import annotations

from typing import Any

__all__ = ["PT_LANG", "INSERT_PT_SQL", "TRUNCATE_PT_SQL", "pt_pairs"]

PT_LANG = "pt"

TRUNCATE_PT_SQL = "TRUNCATE pt_names"

# PK is (printed_name, oracle_id); a name reused across printings of the
# same oracle collapses to one row, so DO NOTHING on conflict.
INSERT_PT_SQL = (
    "INSERT INTO pt_names (printed_name, oracle_id) "
    "VALUES (:printed_name, :oracle_id) ON CONFLICT DO NOTHING"
)


def pt_pairs(card: dict[str, Any]) -> list[tuple[str, str]]:
    """Project one ``all_cards`` row to its ``(printed_name, oracle_id)`` pairs.

    Returns an empty list for non-Portuguese printings, a row missing an
    ``oracle_id``, or one with no Portuguese spelling anywhere. Names are
    de-duplicated within the card (a DFC whose faces share a printed name
    yields it once).
    """
    if card.get("lang") != PT_LANG:
        return []
    oracle_id = card.get("oracle_id")
    if not oracle_id:
        return []

    names: list[str] = []
    seen: set[str] = set()

    def _add(value: Any) -> None:
        if isinstance(value, str):
            name = value.strip()
            if name and name not in seen:
                seen.add(name)
                names.append(name)

    _add(card.get("printed_name"))
    for face in card.get("card_faces") or []:
        if isinstance(face, dict):
            _add(face.get("printed_name"))

    return [(name, str(oracle_id)) for name in names]
