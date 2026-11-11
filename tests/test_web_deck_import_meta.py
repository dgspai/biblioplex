"""D19.26 — the import form carries the metadata the API already accepts."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.web_assets import app_js

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "web" / "index.html"


@pytest.fixture(scope="module")
def js() -> str:
    return app_js()


@pytest.fixture(scope="module")
def html() -> str:
    return INDEX.read_text()


def _import_handler(js: str) -> str:
    start = js.index('deckImportFormEl.addEventListener("submit"')
    return js[start : js.index("\n  });", start)]


# ---- markup ----


def test_all_three_url_inputs_exist(html: str) -> None:
    for el in ("deckImportDeckbox", "deckImportMoxfield", "deckImportArchidekt"):
        assert f'id="{el}"' in html, el


def test_description_input_exists(html: str) -> None:
    assert 'id="deckImportNotes"' in html


def test_metadata_is_behind_a_collapsed_disclosure(html: str) -> None:
    """The common case stays a file picker and one button.

    Reuses the `<details>` pattern the paste box already uses rather than
    inventing a second disclosure interaction.
    """
    block = html[html.index('id="deckImportForm"') :]
    block = block[: block.index("</form>")]
    details = [m.start() for m in re.finditer(r"<details", block)]
    assert len(details) >= 2, "metadata fields need their own <details>"
    assert block.index('id="deckImportDeckbox"') > details[0]


def test_url_inputs_are_typed_as_urls(html: str) -> None:
    """Native validation and mobile keyboards for free — rung 3."""
    block = html[html.index('id="deckImportForm"') :]
    block = block[: block.index("</form>")]
    for el in ("deckImportDeckbox", "deckImportMoxfield", "deckImportArchidekt"):
        row = block[block.index(f'id="{el}"') - 120 : block.index(f'id="{el}"') + 120]
        assert 'type="url"' in row, el


def test_every_input_is_labelled(html: str) -> None:
    """Accessibility basics are not a place to be lazy."""
    block = html[html.index('id="deckImportForm"') :]
    block = block[: block.index("</form>")]
    for el in ("deckImportDeckbox", "deckImportMoxfield", "deckImportArchidekt",
               "deckImportNotes"):
        assert f'for="{el}"' in block, el


# ---- both import paths ----


def test_multipart_path_appends_every_field(js: str) -> None:
    """The file upload takes them as Form(...) fields.

    Anchored on the collector, not on inline field names: the names live in
    `DECK_META_INPUTS` precisely so they are declared once.
    """
    handler = _import_handler(js)
    fd = handler[handler.index("new FormData()") :]
    assert "_deckImportMeta()" in fd
    assert "fd.append" in fd


def test_json_paste_path_sends_every_field(js: str) -> None:
    """The paste path takes them as body fields, from the same collector."""
    handler = _import_handler(js)
    payload = handler[handler.index("const payload") :]
    payload = payload[: payload.index("\n")]
    assert "_deckImportMeta()" in payload


def test_the_collector_declares_all_six_fields(js: str) -> None:
    """The single list of field names, asserted where it actually lives.

    D19.35 added `branch` and `message` to the same list rather than a
    second collector.
    """
    block = js[js.index("const DECK_META_INPUTS") :]
    block = block[: block.index("];")]
    for field in (
        "deckbox_url", "moxfield_url", "archidekt_url", "notes", "branch", "message",
    ):
        assert f'"{field}"' in block, field


def test_both_paths_use_one_field_collector(js: str) -> None:
    """Two copies of "which fields exist" is what drifts.

    A shared collector means adding a field later touches one place.
    """
    handler = _import_handler(js)
    assert handler.count("_deckImportMeta(") >= 2


def test_blank_fields_are_omitted_not_sent_empty(js: str) -> None:
    """An empty string is not the same as "unset".

    Sending `deckbox_url: ""` would store an empty string where the column
    means NULL, and D19.4's validator would then have to special-case it.
    """
    fn = js[js.index("function _deckImportMeta(") :]
    fn = fn[: fn.index("\n}\n")]
    assert ".trim()" in fn
    assert "if (" in fn or "&&" in fn, "blank values must be filtered out"


# ---- server owns validation ----


def test_the_422_is_surfaced_not_swallowed(js: str) -> None:
    """D19.4 validates hostnames per field, server-side.

    Re-implementing that check in JS would give two rules that drift; the
    import handler already surfaces `details` from the error envelope.
    """
    handler = _import_handler(js)
    assert "_deckStatus(deckImportStatusEl" in handler
    assert "err.message" in handler


def test_no_client_side_hostname_validation(js: str) -> None:
    """One validator, server-side."""
    fn = js[js.index("function _deckImportMeta(") :]
    fn = fn[: fn.index("\n}\n")]
    for host in ("deckbox.org", "moxfield.com", "archidekt.com"):
        assert host not in fn, f"{host} check belongs on the server"


def test_fields_clear_after_a_successful_import(js: str) -> None:
    """Otherwise the next import silently inherits the last deck's links.

    D19.35: the branch <select> resets to its `reset` value (`"main"`, its
    default option) rather than `""`, which no option carries and would
    leave nothing selected.
    """
    handler = _import_handler(js)
    tail = handler[handler.index("Imported") :]
    assert "DECK_META_INPUTS" in tail, "reset must walk the shared list"
    assert 'el.value = f.reset || ""' in tail
