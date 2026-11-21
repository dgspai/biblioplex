"""Spellbook template requirements ("1x Persist Creature") survive ingest."""

from __future__ import annotations

from biblioplex.ingest.combo_template_queries import TEMPLATE_QUERIES
from biblioplex.ingest.combos import _map_v2_variant, build_rows, template_card_query

_PERSIST = {
    "template": {"id": 7, "name": "Persist Creature", "scryfallQuery": "keyword:persist t:creature"},
    "quantity": 1,
}


def _variant(**extra: object) -> dict[str, object]:
    return {
        "id": "413-7841--5",
        "uses": [{"card": {"name": "Blasting Station"}}, {"card": {"name": "Darksteel Angel"}}],
        **extra,
    }


def test_requires_mapped_from_v2_variant() -> None:
    mapped = _map_v2_variant(_variant(requires=[_PERSIST]))
    assert mapped is not None
    assert mapped["requires"] == [
        {"template_id": 7, "name": "Persist Creature", "query": "keyword:persist t:creature", "quantity": 1}
    ]


def test_no_requires_maps_to_empty_list() -> None:
    mapped = _map_v2_variant(_variant())
    assert mapped is not None
    assert mapped["requires"] == []


def test_prose_only_template_keeps_null_query() -> None:
    req = {"template": {"id": 9, "name": "Haste Enabler", "scryfallQuery": None}, "quantity": 2}
    mapped = _map_v2_variant(_variant(requires=[req]))
    assert mapped is not None
    assert mapped["requires"][0]["query"] is None
    assert mapped["requires"][0]["quantity"] == 2


def test_malformed_requirement_drops_whole_variant() -> None:
    # Dropping just the requirement would make the combo look complete without it.
    assert _map_v2_variant(_variant(requires=[{"template": {"name": "no id"}}])) is None


def test_build_rows_carries_requires() -> None:
    mapped = _map_v2_variant(_variant(requires=[_PERSIST]))
    assert mapped is not None
    rows = build_rows([mapped], {"blasting station": "o1", "darksteel angel": "o2"})
    assert rows[0]["requires"][0]["template_id"] == 7


def test_template_query_compiles_to_oracle_id_select() -> None:
    stmt = template_card_query("keyword:persist t:creature -m<{0}")
    assert stmt is not None
    assert "oracle_id" in str(stmt)


def test_unusable_template_query_is_none() -> None:
    assert template_card_query(None) is None
    assert template_card_query("m<WW") is None
    assert template_card_query("((") is None


def test_our_query_fills_a_template_spellbook_left_blank() -> None:
    req = {"template": {"id": 85, "name": "Haste Enabler", "scryfallQuery": None}, "quantity": 1}
    mapped = _map_v2_variant(_variant(requires=[req]))
    assert mapped is not None
    assert mapped["requires"][0]["query"] == TEMPLATE_QUERIES[85]


def test_every_template_query_of_ours_compiles() -> None:
    assert [i for i, q in TEMPLATE_QUERIES.items() if template_card_query(q) is None] == []
