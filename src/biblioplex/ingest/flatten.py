"""Multi-face flatten per tech_spec.md §3.3.

Scryfall populates the top-level ``oracle_text``, ``type_line``, ``mana_cost``,
and ``colors`` from the FRONT FACE only for ``transform``, ``modal_dfc``,
``adventure``, ``meld``, ``case``, ``prototype``, ``double_faced_token``. For
``split`` and ``flip`` the top-level ``name``/``type_line`` carry both halves
joined with ` // `, while ``oracle_text``/``mana_cost``/``colors`` are
typically null.

To make `o:`, `t:`, `m:` work against the whole card (back faces, adventure
backs, split halves), we populate four plain ``text`` columns —
``name_search``, ``type_search``, ``oracle_search``, ``mana_cost_search`` —
by space-joining the top-level value with every ``card_faces[*].<field>``,
coalescing nulls to empty strings.

``colors_eff`` is ``colors`` when non-null, else the deduped union of
``card_faces[*].colors``. The output is sorted in canonical WUBRG order
(``W,U,B,R,G``) and filtered to that letter set so the §5.4 set-equality
operator (``c=COLORS``) can match via plain array equality — Postgres array
``=`` is ordered, so both ingest and the DSL compiler must agree on the
sort key. See ``src/biblioplex/dsl/compiler.py:_compile_color``.

These columns can't be real Postgres ``GENERATED`` columns because
``jsonb_array_elements`` is forbidden in generated expressions. We compute
them in the ingest path and they get re-derived every ``POST /admin/refresh``.
"""

from __future__ import annotations

from typing import Any

# Fields we space-join across top-level + each face. Each entry maps the
# SOURCE key (top-level / face) to the OUTPUT column name.
_SEARCH_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "name_search"),
    ("type_line", "type_search"),
    ("oracle_text", "oracle_search"),
    ("mana_cost", "mana_cost_search"),
)

# Canonical WUBRG order for `colors_eff` sort. Matches the compiler-side
# sort in `dsl/compiler.py:_compile_color` so set-equality (`c=COLORS`)
# works via plain array `=` (§5.4 drift resolution: set semantics, not
# ordered-array semantics, via one-time ingest sort + matching query sort).
_WUBRG_ORDER = "WUBRG"
_WUBRG_INDEX = {c: i for i, c in enumerate(_WUBRG_ORDER)}


def _coalesce(value: Any) -> str:
    """Return ``value`` as a string, treating ``None``/missing as empty."""
    if value is None:
        return ""
    return str(value)


def _join_field(card: dict[str, Any], faces: list[dict[str, Any]], key: str) -> str:
    """Space-join the top-level ``key`` with each face's ``key``, coalescing nulls."""
    parts: list[str] = [_coalesce(card.get(key))]
    parts.extend(_coalesce(face.get(key)) for face in faces)
    # Preserve empty strings to keep semantics explicit; trim purely-whitespace
    # leftover whitespace at the edges so trgm indexes don't waste space.
    joined = " ".join(parts).strip()
    # If everything was empty / null, return '' so the NOT NULL column is
    # still legally populated.
    return joined


def _colors_eff(card: dict[str, Any], faces: list[dict[str, Any]]) -> list[str]:
    """Compute ``colors_eff`` per §3.3, sorted in canonical WUBRG order.

    * Top-level ``colors`` non-null → use it as the source set.
    * Else union of ``card_faces[*].colors`` (face order ignored).

    In both branches the result is deduped, filtered to ``{W,U,B,R,G}``
    (defensive — Scryfall has never been observed to emit other letters,
    but the spec gives no contract), and sorted WUBRG so the §5.4
    set-equality operator works via plain Postgres array ``=`` (which is
    ordered, hence the matching sort on both sides).
    """
    top = card.get("colors")
    if top is not None:
        source: list[str] = list(top)
    else:
        source = []
        for face in faces:
            face_colors = face.get("colors")
            if not face_colors:
                continue
            source.extend(face_colors)

    seen: set[str] = set()
    filtered: list[str] = []
    for c in source:
        if c in _WUBRG_INDEX and c not in seen:
            seen.add(c)
            filtered.append(c)
    filtered.sort(key=_WUBRG_INDEX.__getitem__)
    return filtered


def flatten_card(card: dict[str, Any]) -> dict[str, Any]:
    """Return ``card`` augmented with the §3.3 search columns + ``colors_eff``.

    The returned dict is a shallow copy: original keys are preserved and the
    five new keys (``name_search``, ``type_search``, ``oracle_search``,
    ``mana_cost_search``, ``colors_eff``) are added on top. Existing values
    are NOT overwritten — flatten is purely additive.

    Args:
        card: A card dict from D1.2 (stream parser) or D1.3 (post-reversible
            split). ``card_faces`` may be missing, ``None``, or an empty
            list — degrades gracefully to single-face behavior.

    Returns:
        A new dict with `_search` columns populated and `colors_eff` resolved.
        ``colors_eff`` is always a list (possibly empty), never ``None`` —
        matches the NOT NULL contract in `schema.sql`.
    """
    faces_raw = card.get("card_faces")
    faces: list[dict[str, Any]] = list(faces_raw) if isinstance(faces_raw, list) else []

    out = dict(card)
    for src_key, dst_key in _SEARCH_FIELDS:
        out[dst_key] = _join_field(card, faces, src_key)
    out["colors_eff"] = _colors_eff(card, faces)
    return out
