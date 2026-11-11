"""Unit tests for the Commander Spellbook combo ingest (D14.1).

DB and HTTP are not exercised. We feed synthetic combo dicts + a
``name -> oracle_id`` resolver map to :func:`build_rows` and assert the
normalization semantics:

* All resolved names → one row with ``oracle_ids`` in input order.
* Any unresolved name → combo skipped entirely (drop-not-crash).
* ``color_identity`` accepts either a string (``"WUBRG"``) or a list
  (``["W","U","B","R","G"]``) — normalized to a lower-alpha uppercase list
  in canonical WUBRG order.
* ``results`` normalizes to ``list[str]`` (accepts ``None``, ``str``,
  ``list``).
* Malformed / missing required fields (id, cards) → combo skipped.
"""

from __future__ import annotations

import gzip
import json

import httpx
import pytest

from biblioplex.ingest.combos import _iter_combo_batches, build_rows


def _resolver(*pairs: tuple[str, str]) -> dict[str, str]:
    """Build a ``lower(name) -> oracle_id`` map from ``(name, uuid)`` pairs."""
    return {name.lower(): oid for name, oid in pairs}


def test_bug17_resolve_names_to_oracle_ids_lowers_on_db() -> None:
    """Bug.17: `resolve_names_to_oracle_ids` folds both operands via DB LOWER.

    Prior shape was `WHERE LOWER(name) = ANY(:lowered)` with `:lowered`
    Python-lowered — locale mismatch could drop non-ASCII names. Fix
    switches to `JOIN unnest(:names::text[]) AS t(n) ON LOWER(c.name) =
    LOWER(t.n)` so Postgres owns both fold operations.
    """
    from sqlalchemy import text

    from biblioplex.ingest.combos import resolve_names_to_oracle_ids

    class _Result:
        def all(self) -> list[object]:
            return []

    captured: list[str] = []

    class _Conn:
        async def execute(self, stmt: object, params: dict[str, object]) -> _Result:
            captured.append(str(stmt))
            return _Result()

        async def __aenter__(self) -> _Conn:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

    class _Eng:
        def connect(self) -> _Conn:
            return _Conn()

    import asyncio

    asyncio.run(resolve_names_to_oracle_ids(_Eng(), ["Séance", "Sol Ring"]))
    assert captured, "expected one SQL round-trip"
    sql = captured[0]
    assert "LOWER(c.name) = LOWER(t.n)" in sql
    assert "ANY(:lowered)" not in sql
    # Silence unused import.
    _ = text


def test_all_cards_resolved_yields_one_row() -> None:
    combos = [
        {
            "id": "c1",
            "cards": ["Isochron Scepter", "Dramatic Reversal"],
            "color_identity": "U",
            "prereqs": "Nonland permanents that tap for >= 3 mana.",
            "steps": "Imprint Dramatic Reversal, activate Scepter.",
            "results": ["Infinite mana"],
        }
    ]
    name_to_oracle = _resolver(
        ("Isochron Scepter", "o1"), ("Dramatic Reversal", "o2")
    )
    rows = build_rows(combos, name_to_oracle)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "c1"
    assert row["oracle_ids"] == ["o1", "o2"]
    assert row["color_identity"] == ["U"]
    assert row["prereqs"] == "Nonland permanents that tap for >= 3 mana."
    assert row["steps"] == "Imprint Dramatic Reversal, activate Scepter."
    assert row["results"] == ["Infinite mana"]


def test_unresolved_card_drops_combo() -> None:
    combos = [
        {
            "id": "c1",
            "cards": ["Isochron Scepter", "Nonexistent Card"],
            "color_identity": "U",
        }
    ]
    name_to_oracle = _resolver(("Isochron Scepter", "o1"))
    rows = build_rows(combos, name_to_oracle)
    assert rows == []


def test_color_identity_accepts_string_or_list() -> None:
    combos = [
        {"id": "c1", "cards": ["A"], "color_identity": "WUB"},
        {"id": "c2", "cards": ["A"], "color_identity": ["W", "U", "B"]},
        {"id": "c3", "cards": ["A"], "color_identity": "wubrg"},
    ]
    name_to_oracle = _resolver(("A", "o1"))
    rows = build_rows(combos, name_to_oracle)
    assert [r["color_identity"] for r in rows] == [
        ["W", "U", "B"],
        ["W", "U", "B"],
        ["W", "U", "B", "R", "G"],
    ]


def test_color_identity_missing_defaults_empty() -> None:
    combos = [{"id": "c1", "cards": ["A"]}]
    name_to_oracle = _resolver(("A", "o1"))
    rows = build_rows(combos, name_to_oracle)
    assert rows[0]["color_identity"] == []


def test_results_normalized_from_string() -> None:
    combos = [{"id": "c1", "cards": ["A"], "results": "Infinite mana"}]
    name_to_oracle = _resolver(("A", "o1"))
    rows = build_rows(combos, name_to_oracle)
    assert rows[0]["results"] == ["Infinite mana"]


def test_results_missing_defaults_empty_list() -> None:
    combos = [{"id": "c1", "cards": ["A"]}]
    name_to_oracle = _resolver(("A", "o1"))
    rows = build_rows(combos, name_to_oracle)
    assert rows[0]["results"] == []


def test_missing_id_skips_combo() -> None:
    combos = [
        {"cards": ["A"]},
        {"id": "", "cards": ["A"]},
        {"id": None, "cards": ["A"]},
    ]
    name_to_oracle = _resolver(("A", "o1"))
    rows = build_rows(combos, name_to_oracle)
    assert rows == []


def test_missing_or_empty_cards_skips_combo() -> None:
    combos = [
        {"id": "c1"},
        {"id": "c2", "cards": []},
        {"id": "c3", "cards": None},
    ]
    name_to_oracle: dict[str, str] = {}
    rows = build_rows(combos, name_to_oracle)
    assert rows == []


def test_name_lookup_case_insensitive() -> None:
    combos = [{"id": "c1", "cards": ["ISOCHRON SCEPTER", "Dramatic Reversal"]}]
    name_to_oracle = _resolver(
        ("Isochron Scepter", "o1"), ("dramatic reversal", "o2")
    )
    rows = build_rows(combos, name_to_oracle)
    assert rows[0]["oracle_ids"] == ["o1", "o2"]


def test_prereqs_and_steps_default_none() -> None:
    combos = [{"id": "c1", "cards": ["A"]}]
    name_to_oracle = _resolver(("A", "o1"))
    rows = build_rows(combos, name_to_oracle)
    assert rows[0]["prereqs"] is None
    assert rows[0]["steps"] is None


def test_multiple_combos_preserve_input_order() -> None:
    combos = [
        {"id": "c1", "cards": ["A"]},
        {"id": "c2", "cards": ["B"]},
        {"id": "c3", "cards": ["C"]},
    ]
    name_to_oracle = _resolver(("A", "o1"), ("B", "o2"), ("C", "o3"))
    rows = build_rows(combos, name_to_oracle)
    assert [r["id"] for r in rows] == ["c1", "c2", "c3"]


def test_duplicate_combo_id_second_dropped() -> None:
    combos = [
        {"id": "c1", "cards": ["A"]},
        {"id": "c1", "cards": ["B"]},
    ]
    name_to_oracle = _resolver(("A", "o1"), ("B", "o2"))
    rows = build_rows(combos, name_to_oracle)
    assert len(rows) == 1
    assert rows[0]["oracle_ids"] == ["o1"]


def test_color_identity_lowered_uppercased() -> None:
    combos = [{"id": "c1", "cards": ["A"], "color_identity": "wub"}]
    name_to_oracle = _resolver(("A", "o1"))
    rows = build_rows(combos, name_to_oracle)
    assert rows[0]["color_identity"] == ["W", "U", "B"]


def test_non_wubrg_color_letters_dropped() -> None:
    combos = [{"id": "c1", "cards": ["A"], "color_identity": "WXY"}]
    name_to_oracle = _resolver(("A", "o1"))
    rows = build_rows(combos, name_to_oracle)
    assert rows[0]["color_identity"] == ["W"]


# ---- Spellbook bulk file (variants.json.gz) + variant mapping ----


def _v2_variant(cid: str, cards: list[str], **extra: object) -> dict[str, object]:
    return {
        "id": cid,
        "uses": [{"card": {"name": n}} for n in cards],
        **extra,
    }


def _bulk(*variants: dict[str, object]) -> bytes:
    """The bulk file's top-level shape: four keys, variants as API items."""
    return json.dumps(
        {"timestamp": "2026-11-08T00:00:00Z", "version": "7", "variants": list(variants), "aliases": []}
    ).encode()


class _Body(httpx.AsyncByteStream):
    """Un-read body: ``httpx.Response(content=...)`` pre-reads, which breaks
    ``aiter_raw()``; a real network response is always a stream."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        yield self._data


def _resp(data: bytes, status: int = 200, **headers: str) -> httpx.Response:
    return httpx.Response(status, stream=_Body(data), headers=headers)


async def _fetch(handler, url: str = "https://example.com/variants.json.gz"):  # type: ignore[no-untyped-def]
    """Collect every ``(batch, raw_count)`` the fetcher yields."""
    out: list[tuple[list[dict[str, object]], int]] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async for item in _iter_combo_batches(client, url):
            out.append(item)
    return out


async def test_bulk_gzip_body_maps_variant_fields() -> None:
    """Gzipped bytes with no Content-Encoding (Scryfall-style) still parse."""
    variant = _v2_variant(
        "v1",
        ["Isochron Scepter", "Dramatic Reversal"],
        identity="U",
        notablePrerequisites="Nonland permanents that tap for ≥3 mana.",
        description="Imprint Reversal, activate Scepter.",
        produces=[
            {"feature": {"name": "Infinite mana"}},
            {"feature": {"name": "Win the game"}},
        ],
    )
    body = gzip.compress(_bulk(variant))
    batches = await _fetch(lambda _: _resp(body))

    combos = [c for batch, _ in batches for c in batch]
    assert len(combos) == 1
    c = combos[0]
    assert c["id"] == "v1"
    assert c["cards"] == ["Isochron Scepter", "Dramatic Reversal"]
    assert c["color_identity"] == "U"
    assert c["prereqs"] == "Nonland permanents that tap for ≥3 mana."
    assert c["steps"] == "Imprint Reversal, activate Scepter."
    assert c["results"] == ["Infinite mana", "Win the game"]


async def test_bulk_content_encoding_gzip_is_read_compressed() -> None:
    """Spellbook's S3 serves `Content-Encoding: gzip` even for identity requests.

    The fetcher asks for identity and keeps the raw gzip on disk (32 MB
    instead of 729 MB), so it must parse the still-compressed body.
    """
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["accept"] = request.headers.get("accept-encoding", "")
        return _resp(
            gzip.compress(_bulk(_v2_variant("v1", ["A"]))), **{"Content-Encoding": "gzip"}
        )

    batches = await _fetch(handler)
    assert [c["id"] for b, _ in batches for c in b] == ["v1"]
    assert seen["accept"] == "identity"


async def test_bulk_plain_json_body_also_parses() -> None:
    """The uncompressed `variants.json` works too."""
    batches = await _fetch(lambda _: _resp(_bulk(_v2_variant("v1", ["A"]))))
    assert [c["id"] for b, _ in batches for c in b] == ["v1"]


async def test_bulk_raw_count_includes_dropped_variants() -> None:
    """Bug.25: raw source count includes variants the mapper dropped."""
    good = _v2_variant("g1", ["A"])
    bad = {"id": "b1", "uses": [{"card": {}}]}  # dropped by _map_v2_variant
    batches = await _fetch(lambda _: _resp(_bulk(good, bad)))
    assert [c["id"] for b, _ in batches for c in b] == ["g1"]
    assert sum(raw for _, raw in batches) == 2


async def test_bulk_batches_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Variants arrive in fixed-size batches so the whole list is never held."""
    import biblioplex.ingest.combos as combos_mod

    monkeypatch.setattr(combos_mod, "_FETCH_BATCH", 2)
    body = _bulk(*(_v2_variant(f"v{i}", ["A"]) for i in range(5)))
    batches = await _fetch(lambda _: _resp(body))
    assert [len(b) for b, _ in batches] == [2, 2, 1]
    assert [raw for _, raw in batches] == [2, 2, 1]


@pytest.mark.parametrize(
    "payload",
    [
        {"count": 1, "next": None, "results": [{"id": "v1"}]},  # old paginated API
        [{"id": "x", "cards": ["A"]}],  # bare list
        {"variants": []},  # empty bulk
    ],
)
async def test_non_bulk_or_empty_source_raises(payload: object) -> None:
    """Anything but a non-empty bulk file fails loudly.

    A stale `COMBOS_SOURCE_URL` still pointing at `/variants/` must not
    look like "Spellbook has zero combos" — raising keeps the previous
    combos table (the stage is best-effort) and logs why.
    """
    with pytest.raises(ValueError, match="variants"):
        await _fetch(lambda _: _resp(json.dumps(payload).encode()))


async def test_bulk_http_error_raises() -> None:
    with pytest.raises(httpx.HTTPStatusError):
        await _fetch(lambda _: _resp(b"", 503))


async def test_bug20_partial_malformed_uses_drops_whole_variant() -> None:
    """Bug.20: any malformed `uses[i]` entry drops the WHOLE variant.

    Prior `_map_v2_variant` shape iterated ``uses`` with a per-entry
    ``continue``, so a variant declaring 3 required cards but shipping
    one malformed ``card`` silently produced a combo with 2 cards —
    a truncated subset of the source's real requirement. Downstream
    ``/decks/combos`` (``combos.oracle_ids <@ deck_oids``) would then
    match that combo against decks missing the third card.
    """
    from biblioplex.ingest.combos import _map_v2_variant

    # 3 cards declared; middle use has no `card` payload at all.
    variant_missing_middle = {
        "id": "bad1",
        "uses": [
            {"card": {"name": "First"}},
            {"quantity": 1},  # no `card` key
            {"card": {"name": "Third"}},
        ],
    }
    assert _map_v2_variant(variant_missing_middle) is None

    # 3 cards declared; last use has `card` but no `name`.
    variant_missing_name = {
        "id": "bad2",
        "uses": [
            {"card": {"name": "First"}},
            {"card": {"name": "Second"}},
            {"card": {}},
        ],
    }
    assert _map_v2_variant(variant_missing_name) is None

    # 3 cards declared; middle use is not a dict.
    variant_non_dict_use = {
        "id": "bad3",
        "uses": [
            {"card": {"name": "First"}},
            "junk",
            {"card": {"name": "Third"}},
        ],
    }
    assert _map_v2_variant(variant_non_dict_use) is None

    # All-good baseline still maps.
    good = _v2_variant("good", ["A", "B", "C"])
    mapped = _map_v2_variant(good)
    assert mapped is not None
    assert mapped["cards"] == ["A", "B", "C"]


async def test_bulk_prereqs_falls_back_to_easy() -> None:
    variant = _v2_variant("v1", ["A"], easyPrerequisites="Just do X.", description="Steps here.")
    batches = await _fetch(lambda _: _resp(_bulk(variant)))
    assert batches[0][0][0]["prereqs"] == "Just do X."
