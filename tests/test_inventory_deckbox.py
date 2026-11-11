"""Unit tests for Deckbox URL extraction and CSV parsing (D18.2)."""

from __future__ import annotations

import pytest

from biblioplex.inventory.deckbox import (
    extract_deckbox_export_url,
    parse_deckbox_csv,
)


def test_extract_deckbox_export_url_bare_id() -> None:
    url = extract_deckbox_export_url("3293466")
    assert url == "https://deckbox.org/sets/3293466/export?format=csv"


def test_extract_deckbox_export_url_full_set_url() -> None:
    url = extract_deckbox_export_url("https://deckbox.org/sets/3293466")
    assert url == "https://deckbox.org/sets/3293466/export?format=csv"


def test_extract_deckbox_export_url_already_export_url() -> None:
    url = extract_deckbox_export_url("https://deckbox.org/sets/3293466/export?format=csv")
    assert url == "https://deckbox.org/sets/3293466/export?format=csv"


def test_extract_deckbox_export_url_invalid_raises() -> None:
    with pytest.raises(ValueError, match="Invalid Deckbox URL or Set ID"):
        extract_deckbox_export_url("invalid-no-digits")


def test_parse_deckbox_csv_standard_headers() -> None:
    csv_data = (
        "Count,Tradelist Count,Name,Edition,Card Number,Condition,Language,Foil\n"
        "2,0,Sol Ring,Commander 2021,123,Near Mint,English,\n"
        "1,0,Counterspell,Ice Age,45,Good,English,1\n"
    )
    items, warnings = parse_deckbox_csv(csv_data)
    assert len(items) == 2
    assert items[0].card_name == "Sol Ring"
    assert items[0].quantity == 2
    assert items[0].foil_quantity == 0
    assert items[0].set_name == "Commander 2021"

    assert items[1].card_name == "Counterspell"
    assert items[1].quantity == 1
    assert items[1].foil_quantity == 1
    assert items[1].set_name == "Ice Age"
    assert warnings == []


def test_parse_deckbox_csv_empty_or_zero_qty() -> None:
    csv_data = (
        "Count,Name,Edition\n"
        "0,Black Lotus,Alpha\n"
        "3,Lightning Bolt,Revised\n"
    )
    items, warnings = parse_deckbox_csv(csv_data)
    assert len(items) == 1
    assert items[0].card_name == "Lightning Bolt"
    assert items[0].quantity == 3


class _FakeResp:
    """Minimal httpx.Response stand-in for `_assert_csv_body`."""

    def __init__(self, text: str, content_type: str) -> None:
        self.text = text
        self.headers = {"content-type": content_type}


def test_html_login_page_is_rejected_not_parsed() -> None:
    """Deckbox serves `/sets/<id>/export` to anonymous callers as a 200 HTML
    login page. Parsed as CSV it yields zero cards, and a `mode: replace`
    sync would then wipe the inventory and report success."""
    from biblioplex.inventory.deckbox import DeckboxFetchError, _assert_csv_body

    resp = _FakeResp("<!DOCTYPE html>\n<html><body>Login</body></html>", "text/html")
    with pytest.raises(DeckboxFetchError, match="returned HTML, not CSV"):
        _assert_csv_body(resp, "https://deckbox.org/sets/1/export?format=csv")


def test_html_without_content_type_is_still_rejected() -> None:
    from biblioplex.inventory.deckbox import DeckboxFetchError, _assert_csv_body

    resp = _FakeResp("<html><body>Login</body></html>", "application/octet-stream")
    with pytest.raises(DeckboxFetchError):
        _assert_csv_body(resp, "https://deckbox.org/sets/1/export?format=csv")


def test_real_csv_body_passes_through() -> None:
    from biblioplex.inventory.deckbox import _assert_csv_body

    body = "Count,Name,Edition\n1,Sol Ring,Alpha\n"
    resp = _FakeResp(body, "text/csv; charset=utf-8")
    assert _assert_csv_body(resp, "https://deckbox.org/x") == body


def test_structural_parse_failures_are_flagged_fatal() -> None:
    from biblioplex.inventory.deckbox import is_fatal_warning

    for bad in ("", "Foo,Bar\n1,2\n"):
        items, warnings = parse_deckbox_csv(bad)
        assert items == []
        assert warnings and is_fatal_warning(warnings[0])


def test_row_level_warning_is_not_fatal() -> None:
    from biblioplex.inventory.deckbox import is_fatal_warning

    items, warnings = parse_deckbox_csv("Count,Name\nabc,Sol Ring\n")
    assert len(items) == 1
    assert warnings and not is_fatal_warning(warnings[0])
