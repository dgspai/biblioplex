"""Tests for the bulk stream parser (D1.2, tech_spec.md §4.2 step 2).

The stream parser reads the gzipped default_cards JSON dump via ``ijson``,
drops dropped-layout extras (``token``, ``emblem``, ``scheme``, ``planar``,
``vanguard``, ``art_series``, ``double_faced_token``), and yields every
other card object unchanged.

Tests cover:
* Single-shot fixture covering all dropped + kept layouts.
* Property test (spec-mandated by D1.2 brief) over hypothesis-generated
  fixtures of mixed cards — only kept layouts make it through.
* The parser is a true streaming generator: doesn't buffer the whole file.
"""

from __future__ import annotations

import gzip
import io
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from biblioplex.ingest.stream import (
    DROPPED_LAYOUTS,
    iter_cards,
    iter_cards_async,
)


def _encode_array(cards: list[dict[str, Any]]) -> io.BytesIO:
    """Encode card dicts as a JSON Lines byte stream (Bug.34).

    Name kept so the existing tests read unchanged; the wire format under
    them is now one object per line, not a top-level array.
    """
    body = "\n".join(json.dumps(c) for c in cards).encode("utf-8")
    return io.BytesIO(body)


def _encode_gzipped(cards: list[dict[str, Any]]) -> io.BytesIO:
    """Same, but gzipped — how Scryfall actually serves it now."""
    body = "\n".join(json.dumps(c) for c in cards).encode("utf-8")
    return io.BytesIO(gzip.compress(body))


def test_iter_cards_drops_extras_keeps_real_cards() -> None:
    """All seven dropped layouts are filtered; everything else passes through."""
    fixture: list[dict[str, Any]] = [
        {"id": "k1", "layout": "normal", "name": "Llanowar Elves"},
        {"id": "t1", "layout": "token", "name": "Soldier Token"},
        {"id": "k2", "layout": "transform", "name": "Delver of Secrets"},
        {"id": "e1", "layout": "emblem", "name": "Emblem"},
        {"id": "k3", "layout": "modal_dfc", "name": "Valki, God of Lies"},
        {"id": "s1", "layout": "scheme", "name": "Scheme"},
        {"id": "k4", "layout": "split", "name": "Fire // Ice"},
        {"id": "p1", "layout": "planar", "name": "Plane"},
        {"id": "k5", "layout": "adventure", "name": "Brazen Borrower"},
        {"id": "v1", "layout": "vanguard", "name": "Vanguard"},
        {"id": "k6", "layout": "meld", "name": "Bruna, the Fading Light"},
        {"id": "a1", "layout": "art_series", "name": "Art"},
        {"id": "k7", "layout": "reversible_card", "name": "Heads // Tails"},
        {"id": "d1", "layout": "double_faced_token", "name": "DFC Token"},
        {"id": "k8", "layout": "flip", "name": "Akki Lavarunner"},
    ]
    stream = _encode_array(fixture)

    kept = list(iter_cards(stream))
    kept_ids = {c["id"] for c in kept}

    assert kept_ids == {"k1", "k2", "k3", "k4", "k5", "k6", "k7", "k8"}
    for c in kept:
        assert c["layout"] not in DROPPED_LAYOUTS


def test_iter_cards_drops_type_line_card_helpers() -> None:
    """Scryfall helper entries (`type_line == "Card"`) are filtered out.

    These cover ``Red Mana``, ``Poison Counter``, ``Experience``, and
    similar token / box-topper substitutes whose ``type_line`` is the
    literal string ``"Card"``. They polluted classify FPs in the
    2026-06-22 zero-shot run; the filter prevents them from re-entering.
    """
    fixture = [
        {"id": "k1", "layout": "normal", "type_line": "Creature — Elf", "name": "Llanowar Elves"},
        {"id": "h1", "layout": "normal", "type_line": "Card", "name": "Red Mana"},
        {"id": "h2", "layout": "normal", "type_line": "Card", "name": "Poison Counter"},
        {"id": "k2", "layout": "normal", "type_line": "Instant", "name": "Lightning Bolt"},
    ]
    kept_ids = {c["id"] for c in iter_cards(_encode_array(fixture))}
    assert kept_ids == {"k1", "k2"}


def test_iter_cards_empty_array() -> None:
    """Empty array yields nothing — no crash."""
    assert list(iter_cards(_encode_array([]))) == []


def test_iter_cards_passes_full_object_through() -> None:
    """Yielded cards are the full original dicts, not stripped projections."""
    card = {
        "id": "abc",
        "oracle_id": "oid-abc",
        "name": "Test",
        "layout": "normal",
        "type_line": "Creature",
        "card_faces": [{"name": "Test", "mana_cost": "{1}{G}"}],
        "colors": ["G"],
    }
    out = list(iter_cards(_encode_array([card])))
    assert out == [card]


def test_iter_cards_handles_missing_layout_field() -> None:
    """A card without `layout` is conservatively kept (not silently dropped).

    The §4.2 step-2 filter is opt-in by listed layout names. Missing layout
    is unusual for Scryfall bulk data but should propagate to staging so
    downstream code (D1.5) can fail loudly rather than silently swallow.
    """
    card = {"id": "x", "name": "No Layout"}
    out = list(iter_cards(_encode_array([card])))
    assert out == [card]


# --- Property test (spec-mandated by D1.2 brief) ---

_KEPT_LAYOUTS = (
    "normal",
    "transform",
    "modal_dfc",
    "split",
    "flip",
    "adventure",
    "meld",
    "case",
    "prototype",
    "reversible_card",
    "leveler",
    "saga",
    "class",
)

_layout_strategy = st.sampled_from(list(DROPPED_LAYOUTS) + list(_KEPT_LAYOUTS))


@st.composite
def _card(draw: st.DrawFn) -> dict[str, Any]:
    return {
        "id": draw(st.text(min_size=1, max_size=20)),
        "layout": draw(_layout_strategy),
        "name": draw(st.text(min_size=1, max_size=30)),
    }


@given(cards=st.lists(_card(), min_size=0, max_size=40))
@settings(max_examples=75, suppress_health_check=[HealthCheck.too_slow])
def test_property_only_kept_layouts_survive(cards: list[dict[str, Any]]) -> None:
    """For any synthetic mix of dropped + kept layouts, exactly the kept ones pass.

    Invariants:
    * Every yielded card's layout is NOT in DROPPED_LAYOUTS.
    * Yielded count equals input count minus dropped count.
    * Order preserved (we yield in stream order).
    """
    expected = [c for c in cards if c.get("layout") not in DROPPED_LAYOUTS]
    out = list(iter_cards(_encode_array(cards)))

    assert len(out) == len(expected)
    for got, want in zip(out, expected, strict=True):
        assert got == want
        assert got.get("layout") not in DROPPED_LAYOUTS


# Bug.34 — Scryfall migrated the bulk files from a single JSON array served
# as `*.json` with `Content-Encoding: gzip`, to JSON Lines served as
# `*.jsonl.gz` with `Content-Type: application/gzip` and NO
# `Content-Encoding`. Three things changed at once: the metadata key, the
# wire format, and who is responsible for decompression.


def test_gzipped_jsonl_is_decompressed_by_the_sniff() -> None:
    """The bytes arrive still gzipped now — httpx will not do it for us."""
    cards = [
        {"id": "a", "layout": "normal", "name": "Llanowar Elves"},
        {"id": "b", "layout": "token", "name": "Dropped"},
    ]
    kept = list(iter_cards(_encode_gzipped(cards)))
    assert [c["name"] for c in kept] == ["Llanowar Elves"]


def test_plaintext_jsonl_still_works_without_gzip() -> None:
    """Sniffing means a pre-decompressed body needs no flag."""
    cards = [{"id": "a", "layout": "normal", "name": "Bolt"}]
    assert [c["name"] for c in iter_cards(_encode_array(cards))] == ["Bolt"]


def test_trailing_line_without_newline_is_not_lost() -> None:
    """The last object has no terminating \\n — an off-by-one here would
    silently drop one card from every ingest."""
    body = io.BytesIO(b'{"id":"a","layout":"normal","name":"First"}\n'
                      b'{"id":"b","layout":"normal","name":"Last"}')
    assert [c["name"] for c in iter_cards(body)] == ["First", "Last"]


def test_blank_lines_are_skipped() -> None:
    body = io.BytesIO(b'{"id":"a","layout":"normal","name":"A"}\n\n\n'
                      b'{"id":"b","layout":"normal","name":"B"}\n')
    assert [c["name"] for c in iter_cards(body)] == ["A", "B"]


@pytest.mark.asyncio
async def test_async_iter_reassembles_lines_split_across_chunks() -> None:
    """A ~5 KB card object routinely straddles a network chunk boundary.
    Splitting per-chunk instead of on a carry-over buffer would corrupt it."""
    cards = [
        {"id": f"id{i}", "layout": "normal", "name": f"Card{i}", "pad": "x" * 200}
        for i in range(40)
    ]
    body = "\n".join(json.dumps(c) for c in cards).encode()
    chunk_size = 37  # deliberately coprime-ish with the line length

    async def chunks() -> AsyncIterator[bytes]:
        for i in range(0, len(body), chunk_size):
            yield body[i : i + chunk_size]

    got = [c["name"] async for c in iter_cards_async(chunks())]
    assert got == [f"Card{i}" for i in range(40)]


@pytest.mark.asyncio
async def test_async_iter_handles_gzip_across_chunks_and_drops_extras() -> None:
    cards = [
        {"id": "a", "layout": "normal", "name": "Keep"},
        {"id": "t", "layout": "token", "name": "Token"},
        {"id": "c", "layout": "normal", "type_line": "Card", "name": "Helper"},
        {"id": "b", "layout": "modal_dfc", "name": "Keep2"},
    ]
    body = gzip.compress("\n".join(json.dumps(c) for c in cards).encode())

    async def chunks() -> AsyncIterator[bytes]:
        for i in range(0, len(body), 8):
            yield body[i : i + 8]

    got = [c["name"] async for c in iter_cards_async(chunks())]
    assert got == ["Keep", "Keep2"]
