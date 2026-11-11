"""D19.35 — branch + message on import, and the branch-head navigator.

Three surfaces, all reusing D19.6/D19.22/D19.26/D19.34's own plumbing
rather than duplicating it: the import form's `_deckImportMeta()`
collector gains two fields; the version timeline stays ONE interleaved
list with a branch chip and message per row, filterable by a chip click;
and the card table's data source can be pointed at a specific branch's own
latest version without ever moving the deck's head pointer.
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


@pytest.fixture(scope="module")
def js() -> str:
    return app_js()


@pytest.fixture(scope="module")
def html() -> str:
    return INDEX.read_text()


def _fn(js: str, name: str) -> str:
    start = js.index(f"function {name}(")
    return js[start : js.index("\n}\n", start)]


# ---- import form ----


def test_branch_select_exists_with_all_four_branches(html: str) -> None:
    block = html[html.index('id="deckImportForm"') :]
    block = block[: block.index("</form>")]
    assert 'id="deckImportBranch"' in block
    select = block[block.index('id="deckImportBranch"') :]
    select = select[: select.index("</select>")]
    for branch in ("main", "deckbox", "moxfield", "archidekt"):
        assert f'value="{branch}"' in select, branch
    assert 'value="main" selected' in select, "main is the sane default"


def test_message_input_exists_and_is_labelled(html: str) -> None:
    block = html[html.index('id="deckImportForm"') :]
    block = block[: block.index("</form>")]
    assert 'id="deckImportMessage"' in block
    assert 'for="deckImportMessage"' in block


def test_branch_and_message_are_behind_the_same_disclosure_as_the_urls(html: str) -> None:
    """Same collapsed `…more details` panel D19.26 already built."""
    block = html[html.index('deck-import-meta') :]
    block = block[: block.index("</details>")]
    assert 'id="deckImportBranch"' in block
    assert 'id="deckImportMessage"' in block


# ---- one collector, not a second copy ----


def test_branch_head_nav_and_import_share_one_branch_list(js: str) -> None:
    """`DECK_BRANCHES` backs the import select, the timeline filter chips,
    and the branch-head navigator — one definition, not three."""
    assert js.count("const DECK_BRANCHES") == 1
    block = js[js.index("const DECK_BRANCHES") :]
    block = block[: block.index("];")]
    for branch in ("main", "deckbox", "moxfield", "archidekt"):
        assert f'"{branch}"' in block, branch
    assert "DECK_BRANCHES" in _fn(js, "_renderTimelineFilterChips")
    assert "DECK_BRANCHES" in _fn(js, "_renderBranchNav")


# ---- version timeline: one interleaved list, not one per branch ----


def test_timeline_renders_a_branch_chip_and_message_per_row(js: str) -> None:
    fn = _fn(js, "_renderVersionRows")
    assert "v.branch" in fn
    assert "v.message" in fn


def test_timeline_stays_one_list_ordered_by_the_fetched_order(js: str) -> None:
    """No re-sort, no per-branch grouping — `_deckVersions` (already
    version DESC from the API) is filtered, never partitioned."""
    fn = _fn(js, "_renderVersionRows")
    assert ".sort(" not in fn
    assert "_deckTimelineFilter" in fn


def test_filter_chips_are_a_click_not_a_layout(js: str) -> None:
    fn = _fn(js, "_renderTimelineFilterChips")
    assert "data-timeline-branch" in fn


def test_current_badge_follows_head_not_merely_the_newest_number(js: str) -> None:
    """A pinned deck can have newer writes on OTHER branches that never
    moved the pointer (D19.34) — the "current" badge must follow
    `_deckDetailData.version`, or it lies about which version is current
    on a pinned deck."""
    fn = _fn(js, "_renderVersionRows")
    assert "_deckDetailData" in fn
    assert "headVersion" in fn


# ---- branch-head navigator: view, never promote ----


def test_nav_renders_head_plus_every_branch(js: str) -> None:
    fn = _fn(js, "_renderBranchNav")
    assert "data-nav-head" in fn
    assert "data-nav-branch" in fn


def test_viewing_a_branch_never_calls_the_promote_route(js: str) -> None:
    """`POST /decks/{id}/head` is the ONLY thing allowed to move the
    pointer (D19.34). The navigator must never call it."""
    fn = _fn(js, "_renderBranchNav")
    assert "/head" not in fn
    load_fn = _fn(js, "_loadDeckCards")
    assert "/head" not in load_fn


def test_version_param_only_sent_when_viewing_a_branch(js: str) -> None:
    """Omitted `version` -> HEAD via the derived index (unchanged D19.20
    behaviour); a truthy `version` -> the live per-version read (D19.35)."""
    fn = _fn(js, "_loadDeckCards")
    assert "version ? " in fn or "if (version)" in fn
    assert "?version=" in fn


def test_head_pinned_renders_as_a_visible_state(js: str) -> None:
    """A pinned deck whose head has stopped following imports must say
    so, or it reads as a bug rather than a deliberate promote."""
    fn = _fn(js, "_renderDeckDetail")
    assert "head_pinned" in fn
    assert "deck-head-pinned" in fn


def test_click_handler_wires_nav_and_timeline_chips(js: str) -> None:
    start = js.index("if (deckDetailEl)")
    block = js[start : js.index("\nif (tabDecksEl)", start)]
    assert "data-nav-head" in block
    assert "data-nav-branch" in block
    assert "data-timeline-branch" in block


# ---- pure logic, node-exercised ----


def test_branch_head_version_lookup(js: str, tmp_path: Path) -> None:
    """`_deckBranchHeadVersion` reads whatever `_deckVersions` holds — the
    API's own version-DESC order means the first match per branch IS that
    branch's own head. Exercised under node since it needs no DOM."""
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node not available")
    # `_fn` slices up to (not including) the closing brace — fine for the
    # structural pins above, but this one EXECUTES the function, so the
    # brace has to come back (same +3 pattern D19.29's node test uses).
    start = js.index("function _deckBranchHeadVersion(")
    fn = js[start : js.index("\n}\n", start) + 3]
    script = tmp_path / "branch_head.js"
    script.write_text(
        f"""
let _deckVersions = [
  {{ version: 5, branch: "main" }},
  {{ version: 4, branch: "deckbox" }},
  {{ version: 3, branch: "main" }},
  {{ version: 2, branch: "moxfield" }},
  {{ version: 1, branch: "main" }},
];
{fn}
const fail = (m) => {{ console.error(m); process.exit(1); }};
if (_deckBranchHeadVersion("main") !== 5) fail("main head must be the highest main version, got " + _deckBranchHeadVersion("main"));
if (_deckBranchHeadVersion("deckbox") !== 4) fail("deckbox head wrong");
if (_deckBranchHeadVersion("moxfield") !== 2) fail("moxfield head wrong");
if (_deckBranchHeadVersion("archidekt") !== null) fail("a branch with no versions must be null, not 0 or undefined");
"""
    )
    proc = subprocess.run([node, str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


# ---- the whack-a-mole guard (D19.22's lesson, again) ----


def test_every_helper_the_navigator_calls_is_defined(js: str) -> None:
    """`node --check` validates syntax, not identifier existence. D19.22
    shipped a call to a function that did not exist through a green
    check-web; same scan, applied to every function this deliverable
    touches or adds."""
    defined = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", js))
    defined |= set(
        re.findall(r"^(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:\(|async|function)", js, re.M)
    )
    builtins = {
        "if", "for", "while", "switch", "catch", "return", "typeof",
        "String", "Number", "Array", "Object", "Set", "Map", "Promise",
        "JSON", "parseInt", "parseFloat", "encodeURIComponent", "fetch",
        "setTimeout", "console", "isNaN", "alert", "confirm", "Date",
    }
    for fname in (
        "_renderTimelineFilterChips",
        "_renderVersionRows",
        "_renderBranchNav",
        "_deckBranchHeadVersion",
        "_loadDeckCards",
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


def test_deck_view_state_resets_on_deck_switch(js: str) -> None:
    """Otherwise selecting a different deck could keep an old branch
    filter or navigator selection that has no meaning for it."""
    fn = _fn(js, "_loadDeck")
    assert "_deckTimelineFilter = null" in fn
    assert "_deckViewBranch = null" in fn
