"""Tests for the dict-walk projector (D7.2, §7).

The projector takes a card payload and a list of parsed paths and
returns a nested dict containing only the requested fields. Missing
paths (any segment unreachable on a given card) emit ``null`` — the
spec mandates this for shape stability so agents can pattern-match on
field presence without branching on absence.
"""

from __future__ import annotations

from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from biblioplex.projection import parse_projection, project
from biblioplex.projection.whitelist import TOP_LEVEL_FIELDS

# ---- happy paths ----


def test_single_top_level_field() -> None:
    row: dict[str, Any] = {"name": "Llanowar Elves", "type_line": "Creature"}
    assert project(row, [["name"]]) == {"name": "Llanowar Elves"}


def test_multiple_top_level_fields() -> None:
    row: dict[str, Any] = {"name": "X", "type_line": "Y", "cmc": 1}
    assert project(row, [["name"], ["type_line"]]) == {
        "name": "X",
        "type_line": "Y",
    }


def test_nested_dict_path() -> None:
    row: dict[str, Any] = {"prices": {"usd": "1.23", "eur": "0.99"}}
    assert project(row, [["prices", "usd"]]) == {"prices": {"usd": "1.23"}}


def test_list_index_path() -> None:
    row: dict[str, Any] = {
        "card_faces": [
            {"name": "Front", "oracle_text": "F"},
            {"name": "Back", "oracle_text": "B"},
        ]
    }
    assert project(row, [["card_faces", 0, "name"]]) == {
        "card_faces": [{"name": "Front"}]
    }


def test_multiple_indices_share_top_segment() -> None:
    """Two paths into the same list index merge under one entry."""
    row: dict[str, Any] = {
        "card_faces": [
            {"name": "Front", "oracle_text": "F", "mana_cost": "{G}"},
        ]
    }
    parsed = parse_projection("card_faces.0.name,card_faces.0.oracle_text")
    assert project(row, parsed) == {
        "card_faces": [{"name": "Front", "oracle_text": "F"}]
    }


def test_two_indices_into_same_list_merge() -> None:
    """``card_faces.0.name`` + ``card_faces.1.name`` keep both positions."""
    row: dict[str, Any] = {
        "card_faces": [
            {"name": "Front"},
            {"name": "Back"},
        ]
    }
    parsed = parse_projection("card_faces.0.name,card_faces.1.name")
    out = project(row, parsed)
    # Implementation may use a sparse-ish list; both names must be present.
    assert out["card_faces"][0] == {"name": "Front"}
    assert out["card_faces"][1] == {"name": "Back"}


# ---- missing paths emit null ----


def test_missing_top_level_emits_null() -> None:
    row: dict[str, Any] = {"name": "X"}
    assert project(row, [["cmc"]]) == {"cmc": None}


def test_missing_nested_key_emits_null() -> None:
    row: dict[str, Any] = {"prices": {"eur": "1"}}
    assert project(row, [["prices", "usd"]]) == {"prices": {"usd": None}}


def test_missing_nested_top_level_emits_null_at_leaf() -> None:
    """Even when the whole subtree is absent, only the leaf is null."""
    row: dict[str, Any] = {"name": "X"}
    out = project(row, [["prices", "usd"]])
    assert out == {"prices": {"usd": None}}


def test_index_out_of_range_emits_null() -> None:
    row: dict[str, Any] = {"card_faces": [{"name": "Front"}]}
    out = project(row, [["card_faces", 5, "name"]])
    # The single entry at index 5 is null (or the leaf is null) — what
    # matters is the leaf reports absence.
    faces = out["card_faces"]
    # Must contain index 5 with a null leaf; intermediate positions can
    # be null too (didn't ask for them).
    assert faces[5] == {"name": None} or faces[5] is None


def test_type_mismatch_walk_into_scalar_emits_null() -> None:
    """Walking ``.foo`` into a string scalar → leaf null, not a crash."""
    row: dict[str, Any] = {"name": "Llanowar"}
    out = project(row, [["name", "foo"]])
    assert out == {"name": {"foo": None}}


def test_string_index_into_list_emits_null() -> None:
    """``card_faces.name`` (no index) → list can't be string-indexed."""
    row: dict[str, Any] = {"card_faces": [{"name": "Front"}]}
    out = project(row, [["card_faces", "name"]])
    assert out == {"card_faces": {"name": None}}


# ---- property test ----


@st.composite
def _row_and_path(draw: st.DrawFn) -> tuple[dict[str, Any], list[list[str | int]]]:
    """Build a random row + a random subset of valid (possibly missing) paths."""
    keys = draw(
        st.lists(
            st.sampled_from(sorted(TOP_LEVEL_FIELDS)),
            min_size=1,
            max_size=4,
            unique=True,
        )
    )
    row: dict[str, Any] = {}
    for k in keys:
        # Mix scalars, dicts, and lists in the row so paths sometimes
        # land on values, sometimes need null-emission.
        row[k] = draw(
            st.one_of(
                st.text(max_size=8),
                st.integers(),
                st.dictionaries(
                    st.text(
                        alphabet=st.characters(
                            whitelist_categories=("Ll",), max_codepoint=0x7E
                        ),
                        min_size=1,
                        max_size=4,
                    ),
                    st.text(max_size=4),
                    max_size=3,
                ),
            )
        )
    # Ask for at least one path per top-level key, possibly with a tail
    # segment that may or may not exist on the value.
    paths: list[list[str | int]] = []
    for k in keys:
        tail_len = draw(st.integers(min_value=0, max_value=2))
        path: list[str | int] = [k]
        for _ in range(tail_len):
            path.append(
                draw(
                    st.text(
                        alphabet=st.characters(
                            whitelist_categories=("Ll",), max_codepoint=0x7E
                        ),
                        min_size=1,
                        max_size=4,
                    )
                )
            )
        paths.append(path)
    return row, paths


@given(_row_and_path())
def test_property_top_level_keys_match_paths(
    rp: tuple[dict[str, Any], list[list[str | int]]],
) -> None:
    """Output top-level keys = exactly the set of requested top-level segments."""
    row, paths = rp
    out = project(row, paths)
    expected_top = {p[0] for p in paths}
    assert set(out.keys()) == expected_top


@given(_row_and_path())
def test_property_missing_paths_resolve_to_null_or_value(
    rp: tuple[dict[str, Any], list[list[str | int]]],
) -> None:
    """Each leaf is either a value from the row or ``None`` (never raised)."""
    row, paths = rp
    # The test passes if ``project`` returns without exception. The
    # value-presence semantics are exercised in the explicit unit tests.
    project(row, paths)
