"""D19.36 — post-import diff in two subtabs.

Reuses D19.6's two-fetch + POST /decks/diff pipeline and D19.23's PR-style
table wholesale. Both bases come from D19.34's `diff_bases` on the import
response — the client never recomputes which version is the base, only
fetches and diffs what the server already decided.
"""

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


def _fn(js: str, name: str) -> str:
    start = js.index(f"function {name}(")
    return js[start : js.index("\n}\n", start)]


# ---- markup ----


def test_import_diff_container_exists_in_the_import_card(html: str) -> None:
    block = html[html.index('id="decksImportH"') :]
    block = block[: block.index("</section>")]
    assert 'id="deckImportDiff"' in block


# ---- no second diff endpoint, no second diff renderer ----


def test_reuses_the_existing_decks_diff_endpoint(js: str) -> None:
    """§spec: "No new diff endpoint and no second diff renderer." Both the
    version-compare form (D19.6) and this deliverable call the SAME
    `/decks/diff` route."""
    assert js.count('"/decks/diff"') == 2


def test_reuses_renderDeckDiff_rather_than_a_second_table_renderer(js: str) -> None:
    fn = _fn(js, "_renderImportDiffTab")
    assert "_renderDeckDiff(" in fn
    # The PR-style markup (`deck-diff-table`, sign cell, etc.) is defined
    # exactly once, inside `_renderDeckDiff` — not duplicated here.
    assert "deck-diff-table" not in fn


def test_import_diff_uses_the_existing_two_fetch_pipeline(js: str) -> None:
    """Fetches the two versions via the existing `GET
    /decks/{id}/versions/{n}` route (D19.6), not a bespoke one."""
    fn = _fn(js, "_renderImportDiffTab")
    assert "/versions/" in fn


# ---- diff_bases drive everything, never recomputed client-side ----


def test_bases_come_from_the_import_responses_diff_bases(js: str) -> None:
    fn = _fn(js, "_showImportDiff")
    assert "created.diff_bases" in fn
    assert "created.imported_version" in fn


def test_null_base_renders_an_explicit_empty_state_not_a_diff_call(js: str) -> None:
    """A null base must short-circuit BEFORE any fetch — diffing an empty
    decklist against real content would report the whole deck as added."""
    fn = _fn(js, "_renderImportDiffTab")
    guard = fn[: fn.index("base === null")]
    assert "await" not in guard, "must check for a null base before fetching"
    assert "_DECK_IMPORT_DIFF_EMPTY" in fn


def test_two_distinct_empty_state_messages(js: str) -> None:
    block = js[js.index("const _DECK_IMPORT_DIFF_EMPTY") :]
    block = block[: block.index("};")]
    assert '"main"' not in block  # keys are bare identifiers, not quoted
    assert "main:" in block and "branch:" in block
    main_msg = re.search(r"main:\s*\"([^\"]+)\"", block).group(1)
    branch_msg = re.search(r"branch:\s*\"([^\"]+)\"", block).group(1)
    assert main_msg != branch_msg


# ---- deduped import: no diff at all ----


def test_deduped_import_shows_no_diff(js: str) -> None:
    """`version_created: false` — nothing changed, so no subtabs, no
    fetches, just a plain statement."""
    fn = _fn(js, "_showImportDiff")
    guard = fn[: fn.index("version_created === false")]
    assert "await" not in guard
    body = fn[fn.index("version_created === false") :]
    body = body[: body.index("return;") + len("return;")]
    assert "_renderImportDiffTab" not in body


# ---- two subtabs, not a merged view ----


def test_two_subtabs_main_and_previous_head(js: str) -> None:
    block = js[js.index("const _DECK_IMPORT_DIFF_TABS") :]
    block = block[: block.index("];")]
    assert '"main"' in block
    assert '"branch"' in block


def test_switching_tabs_repaints_without_a_new_import(js: str) -> None:
    fn = _fn(js, "_renderImportDiffTab")
    assert "state.active = kind" in fn


def test_diff_results_are_cached_per_tab(js: str) -> None:
    """Switching back to an already-fetched tab must not re-POST
    /decks/diff."""
    fn = _fn(js, "_renderImportDiffTab")
    assert "state.cache" in fn


# ---- wiring ----


def test_import_submit_calls_showImportDiff_after_a_successful_import(js: str) -> None:
    start = js.index("if (deckImportFormEl)")
    block = js[start : js.index("\nif (deckDetailEl)", start)]
    assert "_showImportDiff(created.id, created)" in block


def test_diff_panel_cleared_before_a_new_import_starts(js: str) -> None:
    start = js.index("if (deckImportFormEl)")
    block = js[start : js.index("\nif (deckDetailEl)", start)]
    submit_start = block.index('"Importing…"')
    assert 'deckImportDiffEl.innerHTML = ""' in block[submit_start:]


def test_click_handler_wires_the_subtab_buttons(js: str) -> None:
    assert "data-diff-tab" in js
    start = js.index("if (deckImportDiffEl)")
    block = js[start : js.index("\n", js.index("});", start))]
    assert "data-diff-tab" in block


# ---- the whack-a-mole guard (D19.22's lesson, again) ----


def test_every_helper_the_import_diff_calls_is_defined(js: str) -> None:
    defined = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", js))
    defined |= set(
        re.findall(r"^(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:\(|async|function)", js, re.M)
    )
    builtins = {
        "if", "for", "while", "switch", "catch", "return", "typeof",
        "String", "Number", "Array", "Object", "Set", "Map", "Promise",
        "JSON", "parseInt", "parseFloat", "encodeURIComponent", "fetch",
        "setTimeout", "console", "isNaN",
    }
    for fname in ("_showImportDiff", "_renderImportDiffTab", "_renderImportDiffTabs"):
        body = _fn(js, fname)
        body = re.sub(r"//[^\n]*", "", body)
        locals_ = set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", body))
        params = body[body.index("(") + 1 : body.index(")")]
        locals_ |= {p.strip() for p in params.split(",") if p.strip()}
        for call in set(re.findall(r"(?<![.\w$])([a-zA-Z_$][\w$]*)\s*\(", body)):
            assert (
                call in builtins or call in defined or call in locals_
            ), f"{fname} calls undefined {call}()"


def test_boot_stays_the_last_top_level_statement(js: str) -> None:
    """D19.13: no top-level code after `_bootFromUrl()`."""
    tail = js[js.index("_bootFromUrl();") + len("_bootFromUrl();") :]
    assert not re.search(r"(?m)^[A-Za-z_$]", tail), "no top-level code after boot"
