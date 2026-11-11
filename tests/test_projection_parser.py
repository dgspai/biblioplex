"""Tests for the ``?projection=`` query-string parser (D7.1, §7).

The parser turns a comma-separated list of dotted paths into a list of
segment lists. Integer-looking segments are coerced to ``int`` so the
projector can index into list values (``card_faces.0.name``). Validation
covers the spec-mandated rules: top-level key whitelist + depth cap of 6.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from biblioplex.projection import ProjectionError, parse_projection
from biblioplex.projection.whitelist import TOP_LEVEL_FIELDS

# ---- happy paths ----


def test_single_top_level_field() -> None:
    assert parse_projection("name") == [["name"]]


def test_two_fields_comma_separated() -> None:
    assert parse_projection("name,type_line") == [["name"], ["type_line"]]


def test_dotted_path_two_segments() -> None:
    assert parse_projection("prices.usd") == [["prices", "usd"]]


def test_integer_segment_is_coerced() -> None:
    """Digit-only segments become ``int`` so the projector can list-index."""
    assert parse_projection("card_faces.0.name") == [["card_faces", 0, "name"]]


def test_multiple_paths_with_indices() -> None:
    parsed = parse_projection("name,card_faces.0.name,card_faces.1.oracle_text")
    assert parsed == [
        ["name"],
        ["card_faces", 0, "name"],
        ["card_faces", 1, "oracle_text"],
    ]


def test_max_depth_six_is_allowed() -> None:
    """Six segments must be accepted — the cap is on >6, not >=6."""
    # legalities is a flat dict in real cards but the parser doesn't care
    # about value shape, only segment count.
    parsed = parse_projection("legalities.a.b.c.d.e")
    assert parsed == [["legalities", "a", "b", "c", "d", "e"]]


def test_similarity_is_whitelisted() -> None:
    """``similarity`` is the M6 synthetic annotation — must be projectable."""
    assert parse_projection("similarity") == [["similarity"]]


# ---- rejections ----


def test_unknown_top_level_key_rejected() -> None:
    with pytest.raises(ProjectionError):
        parse_projection("not_a_real_field")


def test_oversize_depth_rejected() -> None:
    """Seven segments → 422 trigger."""
    with pytest.raises(ProjectionError):
        parse_projection("legalities.a.b.c.d.e.f")


def test_empty_segment_rejected() -> None:
    """``foo..bar`` has an empty middle segment → invalid."""
    with pytest.raises(ProjectionError):
        parse_projection("card_faces..name")


def test_empty_path_rejected() -> None:
    """Trailing comma → empty path → invalid."""
    with pytest.raises(ProjectionError):
        parse_projection("name,")


def test_whitespace_only_rejected() -> None:
    with pytest.raises(ProjectionError):
        parse_projection("   ")


# ---- property tests ----


@st.composite
def _valid_path(draw: st.DrawFn) -> str:
    head = draw(st.sampled_from(sorted(TOP_LEVEL_FIELDS)))
    tail_count = draw(st.integers(min_value=0, max_value=5))
    tail = []
    for _ in range(tail_count):
        # Mix of string keys and integer indices.
        seg = draw(
            st.one_of(
                st.text(
                    alphabet=st.characters(
                        whitelist_categories=("Ll", "Lu", "Nd"),
                        max_codepoint=0x7E,
                    ),
                    min_size=1,
                    max_size=6,
                ),
                st.integers(min_value=0, max_value=9).map(str),
            )
        )
        tail.append(seg)
    return ".".join([head, *tail])


@given(st.lists(_valid_path(), min_size=1, max_size=4))
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=80)
def test_property_valid_paths_roundtrip(paths: list[str]) -> None:
    """Any well-formed projection string parses to the expected segments."""
    encoded = ",".join(paths)
    parsed = parse_projection(encoded)
    assert len(parsed) == len(paths)
    for original, segments in zip(paths, parsed, strict=True):
        raw_segments = original.split(".")
        # The parser coerces digit-only strings to int — invert that
        # transformation when comparing.
        assert len(segments) == len(raw_segments)
        for got, raw in zip(segments, raw_segments, strict=True):
            if isinstance(got, int):
                assert str(got) == raw
            else:
                assert got == raw


@given(
    st.text(
        alphabet=st.characters(
            whitelist_categories=("Ll",),
            max_codepoint=0x7E,
        ),
        min_size=1,
        max_size=12,
    )
)
def test_property_invalid_top_level_rejected(key: str) -> None:
    """Any single segment NOT in the whitelist must raise ProjectionError."""
    if key in TOP_LEVEL_FIELDS:
        return  # the strategy can land on a real key — skip those.
    with pytest.raises(ProjectionError):
        parse_projection(key)
