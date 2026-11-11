"""D19.37 — import a new version onto an existing deck, and un-pin head.

Two controls, both on the deck DETAIL pane (not the top "Import a deck"
card, which can only ever create a brand-new deck — the D19.36 drift
entry this deliverable resolves): "Import new version" (file via the new
`POST /decks/:id/versions`, paste via the already-shipped
`PATCH /decks/:id`), and an "Un-pin" button beside the `head_pinned` note
that clears a promote via `POST /decks/:id/head {version: null}`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.web_assets import app_js

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def js() -> str:
    return app_js()


def _fn(js: str, name: str) -> str:
    start = js.index(f"function {name}(")
    return js[start : js.index("\n}\n", start)]


# ---- markup ----


def test_version_import_panel_has_file_branch_message_and_paste(js: str) -> None:
    fn = _fn(js, "_renderDeckVersionImport")
    assert 'id="deckVersionFile"' in fn
    assert 'id="deckVersionBranch"' in fn
    assert 'id="deckVersionMessage"' in fn
    assert 'id="deckVersionPasteText"' in fn
    for branch in ("main", "deckbox", "moxfield", "archidekt"):
        assert f'value="{branch}"' in fn, branch


def test_version_import_has_no_site_fetch_control(js: str) -> None:
    """Bug.32 / D14.18 not landed — rendering a control that cannot
    succeed is the D18.19 mistake, declined again here by author decision."""
    fn = _fn(js, "_renderDeckVersionImport")
    for host in ("deckbox.org", "moxfield.com", "archidekt.com", "url"):
        assert host not in fn.lower()


def test_version_import_panel_is_wired_into_deck_detail(js: str) -> None:
    fn = _fn(js, "_renderDeckDetail")
    assert "_renderDeckVersionImport(" in fn


def test_unpin_button_renders_beside_the_pinned_note(js: str) -> None:
    fn = _fn(js, "_renderDeckDetail")
    pinned_block = fn[fn.index("deck.head_pinned") : fn.index("deck.head_pinned") + 400]
    assert "deckUnpinBtn" in pinned_block


# ---- shared collector, not a third one ----


def test_deck_import_meta_is_generic_over_an_input_list(js: str) -> None:
    """`_deckImportMeta` takes an optional `inputs` list, defaulting to
    `DECK_META_INPUTS` — the version-import control passes its own list
    rather than the function growing a second implementation."""
    fn = _fn(js, "_deckImportMeta")
    assert "function _deckImportMeta(inputs)" in fn
    assert "DECK_META_INPUTS" in fn


def test_version_meta_inputs_reuses_the_same_collector(js: str) -> None:
    fn = _fn(js, "_importDeckVersion")
    assert "_deckImportMeta(DECK_VERSION_META_INPUTS)" in fn


def test_version_meta_inputs_declares_branch_and_message_only(js: str) -> None:
    block = js[js.index("const DECK_VERSION_META_INPUTS") :]
    block = block[: block.index("];")]
    assert '"branch"' in block
    assert '"message"' in block
    for key in ("deckbox_url", "moxfield_url", "archidekt_url", "notes"):
        assert f'"{key}"' not in block


# ---- write paths: new file entry point, existing PATCH for paste ----


def test_file_path_posts_to_the_new_versions_endpoint(js: str) -> None:
    fn = _fn(js, "_importDeckVersion")
    assert "/versions`" in fn
    assert '"POST"' in fn or "method: \"POST\"" in fn


def test_paste_path_reuses_the_existing_patch_route(js: str) -> None:
    """No new backend surface for the paste method — D19.34's PATCH,
    already shipped and tested, does the whole job."""
    fn = _fn(js, "_importDeckVersion")
    assert '_deckApiSend(`/decks/${encodeURIComponent(deckId)}`, "PATCH"' in fn
    assert "decklist: pasted" in fn


def test_success_calls_the_existing_show_import_diff(js: str) -> None:
    """The whole point: D19.36's subtabs finally reach a real diff from a
    click, via the SAME renderer — no second diff UI."""
    fn = _fn(js, "_importDeckVersion")
    assert "_showImportDiff(deckId, created)" in fn


def test_form_and_status_reset_after_a_successful_import(js: str) -> None:
    fn = _fn(js, "_importDeckVersion")
    assert "fileEl.value" in fn
    assert "pasteEl.value" in fn
    assert "DECK_VERSION_META_INPUTS" in fn.split("_loadDeck(deckId)")[0]


def test_missing_file_and_paste_is_rejected_before_any_fetch(js: str) -> None:
    fn = _fn(js, "_importDeckVersion")
    guard = fn[: fn.index("fetch(") if "fetch(" in fn else len(fn)]
    assert "!file && !pasted" in guard


# ---- un-pin ----


def test_unpin_posts_version_null_to_the_head_route(js: str) -> None:
    fn = _fn(js, "_unpinDeckHead")
    assert "/head`" in fn
    assert "version: null" in fn


def test_unpin_click_handler_is_wired(js: str) -> None:
    start = js.index("if (deckDetailEl)")
    block = js[start : js.index("\nif (tabDecksEl)", start)]
    assert "deckUnpinBtn" in block
    assert "_unpinDeckHead()" in block


def test_version_import_submit_handler_is_wired(js: str) -> None:
    start = js.index("if (deckDetailEl)")
    block = js[start : js.index("\nif (tabDecksEl)", start)]
    assert "deckVersionImportForm" in block
    assert "_importDeckVersion()" in block


# ---- the whack-a-mole guard (D19.22's lesson, again) ----


def test_every_helper_this_deliverable_touches_is_defined(js: str) -> None:
    """`node --check` validates syntax, not identifier existence."""
    defined = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", js))
    defined |= set(
        re.findall(r"^(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:\(|async|function)", js, re.M)
    )
    builtins = {
        "if", "for", "while", "switch", "catch", "return", "typeof",
        "String", "Number", "Array", "Object", "Set", "Map", "Promise",
        "JSON", "parseInt", "parseFloat", "encodeURIComponent", "fetch",
        "setTimeout", "console", "isNaN", "alert", "confirm", "Date",
        "FormData", "Error",
    }
    for fname in (
        "_renderDeckVersionImport",
        "_importDeckVersion",
        "_unpinDeckHead",
        "_deckImportMeta",
        "_renderDeckDetail",
    ):
        body = _fn(js, fname)
        body = re.sub(r"//[^\n]*", "", body)
        locals_ = set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", body))
        params = body[body.index("(") + 1 : body.index(")")]
        locals_ |= {p.strip() for p in params.split(",") if p.strip()}
        locals_ |= set(re.findall(r"\(\s*([A-Za-z_$][\w$]*)\s*\)\s*=>", body))
        for call in set(re.findall(r"(?<![.\w$])([a-zA-Z_$][\w$]*)\s*\(", body)):
            assert (
                call in builtins or call in defined or call in locals_
            ), f"{fname} calls undefined {call}()"


def test_boot_stays_the_last_top_level_statement(js: str) -> None:
    """D19.13: two TDZ bugs came from top-level code after the boot call."""
    tail = js[js.index("_bootFromUrl();") + len("_bootFromUrl();") :]
    assert not re.search(r"(?m)^[A-Za-z_$]", tail), "no top-level code after boot"
