"""Tests for `_to_staging_row` (D1.9, tech_spec.md §3.1).

`_to_staging_row` projects a flattened card dict to the `STAGING_COLUMNS`
payload shape the staging INSERT consumes. D1.9 adds two columns to that
shape — `edhrec_rank` and `price_usd` — extracted directly from the raw
Scryfall card. This module pins the extraction semantics so the search
router (D4.6) can sort on them.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from biblioplex.ingest.refresh import _parse_usd, _to_staging_row


def _base_card(**overrides: Any) -> dict[str, Any]:
    """Minimal valid card dict — fill in only what each test exercises."""
    card: dict[str, Any] = {
        "id": "00000000-0000-0000-0000-000000000001",
        "oracle_id": "00000000-0000-0000-0000-000000000002",
        "name": "Test",
        "layout": "normal",
        "released_at": "2025-01-01",
    }
    card.update(overrides)
    return card


# --- edhrec_rank ----------------------------------------------------------


def test_to_staging_row_extracts_edhrec_rank() -> None:
    """Scryfall ships `edhrec_rank` as a top-level int; ingest passes it through."""
    row = _to_staging_row(_base_card(edhrec_rank=42))
    assert row["edhrec_rank"] == 42


def test_to_staging_row_edhrec_rank_missing_is_none() -> None:
    """Cards without an EDHrec rank (unranked / non-eternal) land as NULL."""
    row = _to_staging_row(_base_card())
    assert row["edhrec_rank"] is None


# --- price_usd ------------------------------------------------------------


def test_to_staging_row_parses_price_usd_string() -> None:
    """Scryfall ships `prices.usd` as a string; ingest coerces to Decimal."""
    row = _to_staging_row(_base_card(prices={"usd": "1.23"}))
    assert row["price_usd"] == Decimal("1.23")


def test_to_staging_row_price_usd_null_variants_all_none() -> None:
    """Missing `prices`, null `prices`, or null `prices.usd` all → None."""
    row_absent = _to_staging_row(_base_card())
    row_null_prices = _to_staging_row(_base_card(prices=None))
    row_null_usd = _to_staging_row(_base_card(prices={"usd": None}))
    assert row_absent["price_usd"] is None
    assert row_null_prices["price_usd"] is None
    assert row_null_usd["price_usd"] is None


def test_to_staging_row_price_usd_malformed_is_none() -> None:
    """Non-numeric `prices.usd` string (e.g. 'n/a') decays to None, no raise."""
    row = _to_staging_row(_base_card(prices={"usd": "n/a"}))
    assert row["price_usd"] is None


# --- _parse_usd helper directly ------------------------------------------


def test_parse_usd_handles_typical_values() -> None:
    """Helper accepts decimal strings, rejects empty/null/garbage."""
    assert _parse_usd("0.42") == Decimal("0.42")
    assert _parse_usd("100") == Decimal("100")
    assert _parse_usd(None) is None
    assert _parse_usd("") is None
    assert _parse_usd("n/a") is None


# --- §5.1 games passthrough ----------------------------------------------


def test_to_staging_row_extracts_games() -> None:
    """Scryfall ships `games` as a top-level text list; ingest passes verbatim."""
    row = _to_staging_row(_base_card(games=["paper", "mtgo"]))
    assert row["games"] == ["paper", "mtgo"]


def test_to_staging_row_games_missing_is_none() -> None:
    """Cards without a `games` field land as NULL — CTE coalesces at collapse."""
    row = _to_staging_row(_base_card())
    assert row["games"] is None


def test_to_staging_row_games_empty_list_preserved() -> None:
    """Empty `games` list survives as `[]` (NULL only when key absent)."""
    row = _to_staging_row(_base_card(games=[]))
    assert row["games"] == []


# --- §6.1 color_sort_key (canonical WUBRG-cycle position) ----------------


def test_to_staging_row_color_sort_key_mono_white() -> None:
    """Mono-W card lands at cycle position 1."""
    row = _to_staging_row(_base_card(colors=["W"]))
    assert row["color_sort_key"] == 1


def test_to_staging_row_color_sort_key_colorless_when_no_colors() -> None:
    """No `colors` field → colors_eff empty → position 0 (colorless)."""
    row = _to_staging_row(_base_card())
    assert row["color_sort_key"] == 0


def test_to_staging_row_color_sort_key_wubrg_five_color() -> None:
    """All five colors → position 31, regardless of input letter order."""
    row = _to_staging_row(_base_card(colors=["G", "R", "B", "U", "W"]))
    assert row["color_sort_key"] == 31
