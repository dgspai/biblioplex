"""Tests for reversible-card face split (D1.3, tech_spec.md §3.4)."""

from __future__ import annotations

from typing import Any
from uuid import NAMESPACE_OID, uuid5

from biblioplex.ingest.reversible import split_reversible


def _reversible_fixture() -> dict[str, Any]:
    """A synthetic `reversible_card` modeled after Heads I Win, Tails You Lose.

    Per §3.4 each face carries its own oracle_id and is legally a distinct
    card. Bulk-data face objects sometimes lack their own `id`; the split
    must synthesize `<parent>:<i>` in that case.
    """
    return {
        "id": "00000000-0000-0000-0000-000000000aaa",
        "oracle_id": "parent-oracle-not-used",
        "layout": "reversible_card",
        "name": "Heads I Win // Tails You Lose",
        "card_faces": [
            {
                "name": "Heads I Win",
                "oracle_id": "heads-oracle-id",
                "layout": "normal",
                "type_line": "Sorcery",
                "oracle_text": "Heads happens.",
                "mana_cost": "{2}{U}",
                "colors": ["U"],
            },
            {
                "name": "Tails You Lose",
                "oracle_id": "tails-oracle-id",
                "layout": "normal",
                "type_line": "Sorcery",
                "oracle_text": "Tails happens.",
                "mana_cost": "{2}{B}",
                "colors": ["B"],
            },
        ],
    }


def test_split_emits_one_row_per_face() -> None:
    """One reversible card → two staged rows."""
    rows = split_reversible(_reversible_fixture())
    assert len(rows) == 2


def test_split_synthetic_ids_when_face_lacks_id() -> None:
    """Face without `id` → deterministic UUIDv5(`<parent_id>:<face_index>`)."""
    rows = split_reversible(_reversible_fixture())
    parent = "00000000-0000-0000-0000-000000000aaa"
    assert rows[0]["id"] == str(uuid5(NAMESPACE_OID, f"{parent}:0"))
    assert rows[1]["id"] == str(uuid5(NAMESPACE_OID, f"{parent}:1"))


def test_split_uses_face_id_when_present() -> None:
    """Face with its own `id` keeps it, no synthesis."""
    card = _reversible_fixture()
    card["card_faces"][0]["id"] = "face-uuid-zero"
    rows = split_reversible(card)
    assert rows[0]["id"] == "face-uuid-zero"
    # Second face still lacks id → synthetic UUIDv5 fallback.
    assert rows[1]["id"] == str(uuid5(NAMESPACE_OID, f"{card['id']}:1"))


def test_split_uses_face_oracle_id() -> None:
    """oracle_id comes from the face, NOT the parent."""
    rows = split_reversible(_reversible_fixture())
    assert rows[0]["oracle_id"] == "heads-oracle-id"
    assert rows[1]["oracle_id"] == "tails-oracle-id"


def test_split_lifts_face_gameplay_fields() -> None:
    """name, type_line, oracle_text, mana_cost, colors all from the face."""
    rows = split_reversible(_reversible_fixture())

    assert rows[0]["name"] == "Heads I Win"
    assert rows[0]["type_line"] == "Sorcery"
    assert rows[0]["oracle_text"] == "Heads happens."
    assert rows[0]["mana_cost"] == "{2}{U}"
    assert rows[0]["colors"] == ["U"]

    assert rows[1]["name"] == "Tails You Lose"
    assert rows[1]["colors"] == ["B"]


def test_split_rewrites_layout_to_face_layout() -> None:
    """`layout` rewritten from `reversible_card` to the face's own layout."""
    rows = split_reversible(_reversible_fixture())
    assert all(r["layout"] == "normal" for r in rows)


def test_split_card_faces_is_single_element() -> None:
    """card_faces becomes a single-element array containing that face."""
    rows = split_reversible(_reversible_fixture())
    for i, row in enumerate(rows):
        assert isinstance(row["card_faces"], list)
        assert len(row["card_faces"]) == 1
        assert row["card_faces"][0]["name"] == ["Heads I Win", "Tails You Lose"][i]


def test_split_face_layout_falls_back_to_normal() -> None:
    """If a face omits `layout`, default to `normal` (Scryfall convention)."""
    card = _reversible_fixture()
    card["card_faces"][0].pop("layout")
    rows = split_reversible(card)
    assert rows[0]["layout"] == "normal"


def test_split_raises_on_non_reversible() -> None:
    """Caller is responsible for the layout check; this function is opinionated.

    Defensive guard: a non-reversible card passed in is a programming error,
    not a data error. We return the input as a single-element list so the
    refresh runner can keep the call site simple, but assert layout in the
    docstring's responsibility split.
    """
    # By design we expect split_reversible to ONLY be called for reversible
    # cards. Document the contract by checking that a transform card raises.
    import pytest

    with pytest.raises(ValueError):
        split_reversible({"id": "x", "layout": "transform", "card_faces": [{"name": "A"}]})
