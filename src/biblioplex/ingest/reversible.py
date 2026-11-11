"""Reversible-card face split per tech_spec.md §3.4.

`layout = 'reversible_card'` (e.g. *Heads I Win, Tails You Lose*) is the
only layout where each face is legally a distinct card with its own
``oracle_id``. The collapse step keys on ``oracle_id`` (§4.2 step 4), so
we must explode reversible parents into one staged row per face — every
other multi-face layout collapses to a single row per parent.

Inputs come from the stream parser (D1.2). Outputs feed the multi-face
flatten step (D1.4) which then populates the `_search` columns for both
emitted faces independently.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import NAMESPACE_OID, uuid5

REVERSIBLE_LAYOUT = "reversible_card"


def split_reversible(card: dict[str, Any]) -> list[dict[str, Any]]:
    """Emit one staged-row dict per face of a reversible card.

    Args:
        card: A card object whose ``layout`` MUST be ``reversible_card``.
            ``card_faces`` is required and must be a non-empty list.

    Returns:
        A list of card dicts — one per face. Each row has:

        * ``id`` — face's own ``id`` if present, else ``<parent_id>:<i>``.
        * ``oracle_id`` — face's ``oracle_id`` (NOT the parent's).
        * ``layout`` — face's own layout, defaulting to ``normal`` when
          the face omits it (Scryfall serves the face-level layout for
          reversibles but other splits may not).
        * ``name``, ``type_line``, ``oracle_text``, ``mana_cost``,
          ``colors`` — lifted from the face, overriding the parent.
        * ``card_faces`` — single-element list containing the face. This
          keeps face-aware downstream code (D1.4 flatten, search DSL
          `m:`/`o:`) uniform across layouts.
        * All other parent fields (``rarity``, ``legalities``, ``raw``,
          ``released_at``, …) preserved.

    Raises:
        ValueError: If ``card['layout']`` is not ``reversible_card`` or
            ``card_faces`` is missing/empty. Splitting a non-reversible
            card is a caller bug; we fail loud rather than silently
            forwarding.
    """
    if card.get("layout") != REVERSIBLE_LAYOUT:
        raise ValueError(
            f"split_reversible() called on layout={card.get('layout')!r}; "
            "expected 'reversible_card'"
        )

    faces = card.get("card_faces")
    if not isinstance(faces, list) or not faces:
        raise ValueError("reversible card missing non-empty card_faces array")

    parent_id = card.get("id")
    rows: list[dict[str, Any]] = []

    for index, face in enumerate(faces):
        # Start from a deep copy of the parent so all unrelated columns
        # (legalities, all_parts, released_at, …) survive untouched.
        row = deepcopy(card)

        # Face-derived id, with synthetic fallback per §3.4. Schema column
        # is `uuid`, so the fallback is a deterministic UUIDv5 derived from
        # the parent id + face index rather than the literal "<id>:<i>".
        face_id = face.get("id")
        row["id"] = (
            face_id
            if isinstance(face_id, str) and face_id
            else str(uuid5(NAMESPACE_OID, f"{parent_id}:{index}"))
        )

        # oracle_id MUST come from the face for reversibles (parent's is
        # arbitrary and not legally unique).
        row["oracle_id"] = face["oracle_id"]

        # Face-level overrides for gameplay fields.
        row["name"] = face.get("name", card.get("name"))
        row["type_line"] = face.get("type_line")
        row["oracle_text"] = face.get("oracle_text")
        row["mana_cost"] = face.get("mana_cost")
        row["colors"] = face.get("colors")

        # Layout rewrite — typically "normal" for reversibles, but honor
        # face's own value when present.
        row["layout"] = face.get("layout", "normal")

        # card_faces becomes a single-element list. Wrap in a fresh dict
        # so mutations to the row don't leak across iterations.
        row["card_faces"] = [deepcopy(face)]

        rows.append(row)

    return rows
