"""D19.40 — web: the sync strip, "are my copies in sync?".

`GET /decks/{id}/branches` compares folded card rows server-side; this
side only renders `differs`/`is_head` into chips. No client-side fold,
no client-side diff.
"""

from __future__ import annotations

import re
import shutil
import subprocess
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


# ---- markup / wiring ----


def test_sync_strip_container_is_rendered(js: str) -> None:
    fn = _fn(js, "_renderDeckDetail")
    assert 'id="deckSyncStrip"' in fn


def test_load_deck_fetches_branches(js: str) -> None:
    fn = _fn(js, "_loadDeck")
    assert "_loadDeckSyncStrip(deckId)" in fn


def test_sync_strip_fetches_the_branches_endpoint(js: str) -> None:
    fn = _fn(js, "_loadDeckSyncStrip")
    assert "/branches" in fn
    assert "_renderSyncStrip(" in fn


def test_no_client_side_diff_is_computed(js: str) -> None:
    """The fold is server-side (D19.19's `deck_card_rows`) — the client
    only paints `differs`, it never recomputes it."""
    for fname in ("_deckSyncChip", "_renderSyncStrip", "_loadDeckSyncStrip"):
        fn = _fn(js, fname)
        assert "parse_sections" not in fn
        assert "diff_qty_maps" not in fn


# ---- the whack-a-mole guard (D19.22's lesson, again) ----


def test_every_helper_the_sync_strip_calls_is_defined(js: str) -> None:
    """`node --check` validates syntax, not identifier existence."""
    defined = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", js))
    defined |= set(
        re.findall(r"^(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:\(|async|function)", js, re.M)
    )
    builtins = {
        "if", "for", "while", "switch", "catch", "return", "typeof",
        "String", "Number", "Array", "Object", "Set", "Map", "Promise",
        "JSON", "parseInt", "parseFloat", "encodeURIComponent", "fetch",
        "setTimeout", "console", "isNaN", "alert", "confirm",
    }
    for fname in ("_deckSyncChip", "_renderSyncStrip", "_loadDeckSyncStrip"):
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


# ---- rendering semantics, run for real ----


def test_chip_rendering_executes_correctly(js: str, tmp_path: Path) -> None:
    """Substring pins cannot tell "3 cards" apart from "0 cards" — run it."""
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node not available")
    esc = js[js.index("function escapeHtml(") :]
    esc = esc[: esc.index("\n}\n") + 3]
    chip = js[js.index("function _deckSyncChip(") :]
    chip = chip[: chip.index("\n}\n") + 3]
    strip = js[js.index("function _renderSyncStrip(") :]
    strip = strip[: strip.index("\n}\n") + 3]
    script = tmp_path / "sync.js"
    script.write_text(
        esc + "\n" + chip + "\n"
        + """
const $ = (sel) => (sel === "#deckSyncStrip" ? global.__el : null);
global.__el = { innerHTML: "" };
const fail = (m) => { console.error(m); process.exit(1); };

// Head's own row: trivially in sync regardless of `differs`.
const headChip = _deckSyncChip({ branch: "main", version: 3, is_head: true,
  differs: { added: 0, removed: 0, changed: 0 } });
if (!headChip.includes("\\u2713")) fail("head must show in-sync mark");
if (headChip.includes("card")) fail("in-sync chip must not show a card count");

// Text-differs-but-identical: zero differs, not head -> still in sync.
const identicalChip = _deckSyncChip({ branch: "deckbox", version: 2, is_head: false,
  differs: { added: 0, removed: 0, changed: 0 } });
if (!identicalChip.includes("\\u2713")) fail("zero differs must read as in-sync");

// A real difference: 1 added + 1 removed + 1 changed = 3 cards, out of sync.
const diffChip = _deckSyncChip({ branch: "moxfield", version: 5, is_head: false,
  differs: { added: 1, removed: 1, changed: 1 } });
if (!diffChip.includes("\\u2717")) fail("a real difference must show the out-of-sync mark");
if (!diffChip.includes("3 cards")) fail(`expected "3 cards" in ${diffChip}`);

// A single-branch deck renders no strip, not one lone chip.
""" + strip + """
_renderSyncStrip([{ branch: "main", version: 1, is_head: true, differs: { added: 0, removed: 0, changed: 0 } }]);
if (global.__el.innerHTML !== "") fail("a single-branch deck must render an empty strip");
_renderSyncStrip([
  { branch: "main", version: 1, is_head: true, differs: { added: 0, removed: 0, changed: 0 } },
  { branch: "moxfield", version: 2, is_head: false, differs: { added: 0, removed: 0, changed: 3 } },
]);
if (!global.__el.innerHTML.includes("\\u00b7")) fail("two-plus branches must render a joined strip");
"""
    )
    proc = subprocess.run([node, str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
