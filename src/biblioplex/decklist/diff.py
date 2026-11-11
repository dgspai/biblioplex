"""Pure decklist-diff classification (D14.15), reused by D19.40.

One function so `POST /decks/diff` (routers/decks.py, fed a raw-text
fold) and `GET /decks/{id}/branches` (decks/service.py, fed a
`deck_card_rows()` fold) can never disagree about what "added" /
"removed" / "changed" means — there is exactly one differ, handed two
different folds of the same shape.
"""

from __future__ import annotations

from typing import Any

#: `lowered_name -> (display_name, qty)` — the shared fold shape both
#: callers reduce their decklist(s) to before calling `diff_qty_maps`.
QtyMap = dict[str, tuple[str, int]]


def diff_qty_maps(a_map: QtyMap, b_map: QtyMap) -> dict[str, Any]:
    """Classify two folded decklists: what changed from `a` to `b`.

    Returns `added` / `removed` / `qty_changed` (name-sorted) plus
    `unchanged_count`, `a_total`, `b_total` — the exact shape `POST
    /decks/diff` returns under its `deck_diff` envelope, minus the
    envelope itself.
    """
    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    qty_changed: list[dict[str, Any]] = []
    unchanged_count = 0

    for key in a_map.keys() | b_map.keys():
        a_side = a_map.get(key)
        b_side = b_map.get(key)
        if a_side is None and b_side is not None:
            added.append({"name": b_side[0], "qty": b_side[1]})
        elif b_side is None and a_side is not None:
            removed.append({"name": a_side[0], "qty": a_side[1]})
        elif a_side is not None and b_side is not None:
            if a_side[1] == b_side[1]:
                unchanged_count += 1
            else:
                # Display name from B (the "after" side) when both present.
                qty_changed.append({
                    "name": b_side[0],
                    "from": a_side[1],
                    "to": b_side[1],
                    "delta": b_side[1] - a_side[1],
                })

    return {
        "added": sorted(added, key=lambda r: r["name"].lower()),
        "removed": sorted(removed, key=lambda r: r["name"].lower()),
        "qty_changed": sorted(qty_changed, key=lambda r: r["name"].lower()),
        "unchanged_count": unchanged_count,
        "a_total": sum(qty for _, qty in a_map.values()),
        "b_total": sum(qty for _, qty in b_map.values()),
    }
