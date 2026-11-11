"""D19.18 — folder tree for the Decks tab.

Static pins over web/index.html and web/js/*.js. app.js has no JS
unit harness (only helpers.js does, via web/test_helpers.html), so these
guard the structure that makes the behaviour correct; the behaviour itself
is confirmed in a headless browser and recorded in the commit body.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.web_assets import app_js

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "web" / "index.html"
CSS = ROOT / "web" / "style.css"


@pytest.fixture(scope="module")
def js() -> str:
    return app_js()


@pytest.fixture(scope="module")
def html() -> str:
    return INDEX.read_text()


@pytest.fixture(scope="module")
def css() -> str:
    return CSS.read_text()


def _fn(js: str, name: str) -> str:
    start = js.index(f"function {name}(")
    return js[start : js.index("\n}\n", start)]


# ---- markup ----


def test_folder_tree_container_exists(html: str) -> None:
    assert 'id="deckFolders"' in html


# ---- consumes the endpoint, does not re-derive it ----


def test_tree_reads_the_folders_endpoint(js: str) -> None:
    assert "/decks/folders" in js


def test_ancestors_are_not_re_inferred_client_side(js: str) -> None:
    """`GET /decks/folders` already DERIVES ancestors — a deck at
    `Commander/Budget` yields a `Commander` node with deck_count 0.

    Rebuilding that client-side would be a second implementation of the
    same rule, free to disagree with the server's.
    """
    render = _fn(js, "_renderFolderTree")
    assert "total_deck_count" in render, "server-supplied subtree count unused"
    assert "deck_count" in render


def test_parent_shows_direct_and_subtree_counts(js: str) -> None:
    """A parent with no direct decks must still read sensibly."""
    render = _fn(js, "_renderFolderTree")
    assert "total_deck_count" in render and "deck_count" in render


# ---- the three distinct list requests ----


def test_unfiled_bucket_uses_the_empty_folder_query(js: str) -> None:
    """`?folder=` present-but-empty means unfiled; omitting it means no
    filter. Conflating them hides every unfiled deck or shows all of them."""
    assert "folder=" in js
    fn = _fn(js, "_deckFolderQuery")
    # Three-valued selection, expressed as named constants rather than bare
    # null/"" literals — the distinction is too easy to flatten otherwise.
    assert "_FOLDER_UNFILED" in fn
    assert "_FOLDER_ALL" in fn
    assert 'return "&folder=";' in fn, "unfiled must send an EMPTY folder param"


def test_selecting_a_node_requests_the_subtree(js: str) -> None:
    fn = _fn(js, "_deckFolderQuery")
    assert "include_subfolders" in fn


def test_unfiled_decks_are_never_hidden(js: str) -> None:
    """An unfiled deck with no bucket is invisible in the UI entirely."""
    render = _fn(js, "_renderFolderTree")
    assert "_FOLDER_UNFILED" in render, "no unfiled bucket rendered"
    assert "Unfiled" in render
    assert "_deckUnfiledCount" in render, "bucket rendered without its count"


# ---- move and rename ----


def test_rename_calls_the_rename_endpoint(js: str) -> None:
    assert "/decks/folders/rename" in js


def test_move_patches_a_single_deck(js: str) -> None:
    """Moving one deck is a PATCH; renaming moves the whole subtree."""
    fn = _fn(js, "_deckMoveToFolder")
    assert "PATCH" in fn


def test_delete_folder_is_a_rename_with_no_target(js: str) -> None:
    """There is no folder row to delete — unfiling IS deletion."""
    assert "_deckFolderRename" in js


# ---- wiring ----


def test_tree_refreshes_with_the_decks_tab(js: str) -> None:
    """A stale tree after an import or a move is worse than none."""
    loaders = js[js.index("const _TAB_LOADERS") :]
    loaders = loaders[: loaders.index("};")]
    assert "decks:" in loaders
    assert "refreshDeckFolders" in js


def test_boot_remains_the_last_top_level_statement(js: str) -> None:
    """D19.13's temporal-dead-zone class — `let`s are not hoisted, so any
    top-level code after boot can be read before it initialises."""
    call = js.index("\n_bootFromUrl();")
    after = js[call + len("\n_bootFromUrl();") :]
    remaining = [
        line
        for line in after.splitlines()
        if line.strip() and not line.startswith(("//", " ", "\t"))
    ]
    assert not remaining, f"top-level code runs after boot: {remaining[:3]}"


def test_tree_styles_are_theme_token_based(css: str) -> None:
    """Hardcoded colours break the dark palette."""
    block = re.findall(r"\.deck-folder[^{]*\{[^}]*\}", css)
    assert block, "no .deck-folder rules"
    joined = " ".join(block)
    assert "var(--" in joined
    assert not re.search(r":\s*#[0-9a-fA-F]{3,6}\b", joined)


def test_folder_query_builder_executes_correctly(js: str, tmp_path: Path) -> None:
    """Run `_deckFolderQuery` for real rather than grepping its source.

    The three-valued selection (no filter / unfiled / subtree) is the part
    most likely to be flattened by a later refactor, and a substring pin
    would not notice. node is already a hard dependency of `make
    check-web`, so executing the extracted function costs nothing new.
    """
    node = shutil.which("node")
    if node is None:  # pragma: no cover - node is a check-web dependency
        pytest.skip("node not available")

    fn = js[js.index("function _deckFolderQuery()") :]
    fn = fn[: fn.index("\n}\n") + 3]
    script = tmp_path / "q.js"
    script.write_text(
        'const _FOLDER_ALL = null;\nconst _FOLDER_UNFILED = "";\n'
        "let _deckFolderSel;\n" + fn + """
const cases = [
  [_FOLDER_ALL,     ""],
  [_FOLDER_UNFILED, "&folder="],
  ["Legacy",        "&folder=Legacy&include_subfolders=true"],
  ["50%_budget",    "&folder=50%25_budget&include_subfolders=true"],
];
for (const [sel, want] of cases) {
  _deckFolderSel = sel;
  const got = _deckFolderQuery();
  if (got !== want) {
    console.error(`sel=${JSON.stringify(sel)} got ${JSON.stringify(got)} want ${JSON.stringify(want)}`);
    process.exit(1);
  }
}
"""
    )
    proc = subprocess.run([node, str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_list_refresh_also_refreshes_folder_counts(js: str) -> None:
    """F15: after the first import the tree said "All decks 0" until the tab
    was re-entered, because only the tab loader refetched folders. Every path
    that changes the deck set ends in refreshDecksList(), so it owns this."""
    assert "refreshDeckFolders()" in _fn(js, "refreshDecksList")


def test_decks_tab_reentry_reruns_a_shown_evaluation(js: str) -> None:
    """F15: ownership changes elsewhere left the Evaluation panel at "0% owned"."""
    loader = js[js.index("const _TAB_LOADERS"):]
    assert "_refreshDeckEvalIfShown()" in loader[: loader.index("};")]
    assert "_loadDeckEval(_deckSelectedId)" in _fn(js, "_refreshDeckEvalIfShown")
