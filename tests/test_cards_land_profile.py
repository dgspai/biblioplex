"""Card payloads carry ``land_profile`` for lands only."""

from __future__ import annotations

from biblioplex.routers.cards import _decorate


def test_land_payload_has_profile() -> None:
    raw = {"type_line": "Land", "oracle_text": "{T}: Add {C}.", "produced_mana": ["C"]}
    out = _decorate(raw, ["plan"], {})
    assert out["land_profile"] == {"colors": "colorless", "etb": "untapped", "utility": []}


def test_non_land_payload_has_no_profile() -> None:
    out = _decorate({"type_line": "Instant", "oracle_text": "Draw two cards."}, [], {})
    assert "land_profile" not in out
