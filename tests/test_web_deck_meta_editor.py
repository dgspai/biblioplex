"""D19.27 — deck metadata is editable in place, decklist is not."""

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


# ---- the panel ----


def test_editor_renders_every_mutable_field(js: str) -> None:
    """The same names `MUTABLE_FIELDS` exposes, minus decklist.

    Asserted on `DECK_META_FIELDS`, not the render function: the renderer
    is generic over that table, which is why the names are declared once.
    """
    block = js[js.index("const DECK_META_FIELDS") :]
    block = block[: block.index("];")]
    for field in ("name", "format", "folder", "commander", "companion",
                  "notes", "deckbox_url", "moxfield_url", "archidekt_url",
                  "trunk_branch"):
        assert f'"{field}"' in block, field


def test_editor_never_sends_a_decklist(js: str) -> None:
    """D19.22's read-only decision stands.

    A decklist in this PATCH would append a version, which is exactly what
    "no edits, only new syncs" rules out.
    """
    fn = _fn(js, "_deckMetaPayload")
    code = re.sub(r"//[^\n]*", "", fn)
    assert "decklist" not in code


def test_editor_patches(js: str) -> None:
    save = _fn(js, "_saveDeckMeta")
    assert '"PATCH"' in save


def test_links_still_render_when_not_editing(js: str) -> None:
    """URLs are links when set, inputs when editing — `_renderDeckLinks`
    already builds the link strip and must not be duplicated."""
    assert "_renderDeckLinks(" in js


def test_description_is_labelled_description_not_notes(js: str) -> None:
    """`notes` is the column; "Description" is what a user calls it."""
    block = js[js.index("const DECK_META_FIELDS") :]
    block = block[: block.index("];")]
    row = block[block.index('"notes"') : block.index('"notes"') + 120]
    assert "Description" in row


def test_no_description_column_was_invented(js: str) -> None:
    """One field is enough — author confirmed."""
    assert "description_url" not in js
    fn = _fn(js, "_deckMetaPayload")
    assert '"description"' not in fn and "'description'" not in fn


def test_trunk_branch_is_a_select_with_a_follow_everything_option(js: str) -> None:
    """D19.38 — value space is exactly the four branches plus null, so a
    select is correct where every other field here is free text."""
    fn = _fn(js, "_renderDeckMetaEditor")
    assert "<select" in fn
    block = js[js.index("const DECK_META_FIELDS") :]
    block = block[: block.index("];")]
    row = block[block.index('"trunk_branch"') : block.index('"trunk_branch"') + 200]
    assert "select: true" in row
    for branch in ("main", "deckbox", "moxfield", "archidekt"):
        assert f'"{branch}"' in row


# ---- payload semantics ----


def test_blank_fields_are_sent_as_null_not_empty_string(js: str) -> None:
    """Clearing a URL must UNSET it.

    Omitting the key would leave the old value; sending "" would store an
    empty string that D19.4's validator then has to special-case. `null`
    is the only spelling that means "unset" to a nullable column.
    """
    fn = _fn(js, "_deckMetaPayload")
    assert "null" in fn


def test_name_is_never_blanked(js: str) -> None:
    """`decks.name` is NOT NULL — a blank name must not be sent."""
    fn = _fn(js, "_deckMetaPayload")
    assert "name" in fn
    assert "trim()" in fn


def test_payload_executes_correctly(js: str, tmp_path: Path) -> None:
    """Run the blank-to-null mapping for real.

    A substring pin cannot tell `null` from `""`, and the difference is
    whether clearing a link works at all.
    """
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node not available")
    table = js[js.index("const DECK_META_FIELDS") :]
    table = table[: table.index("];") + 2]
    fn = js[js.index("function _deckMetaPayload(") :]
    fn = fn[: fn.index("\n}\n") + 3]
    script = tmp_path / "meta.js"
    script.write_text(
        table + "\n" + fn
        + """
const read = (k) => ({
  name: "Iroh gift", format: "commander", folder: "Legacy/Budget",
  commander: "", companion: "", notes: "a description",
  deckbox_url: "", moxfield_url: "https://moxfield.com/decks/x",
  archidekt_url: "", trunk_branch: "main",
}[k]);
const p = _deckMetaPayload(read);
const fail = (m) => { console.error(m); process.exit(1); };
if (p.name !== "Iroh gift") fail("name lost");
if (p.notes !== "a description") fail("notes lost");
if (p.moxfield_url !== "https://moxfield.com/decks/x") fail("url lost");
if (p.deckbox_url !== null) fail(`blank url must be null, got ${JSON.stringify(p.deckbox_url)}`);
if (p.commander !== null) fail(`blank commander must be null, got ${JSON.stringify(p.commander)}`);
if (p.trunk_branch !== "main") fail("trunk_branch lost");
if ("decklist" in p) fail("payload must never carry a decklist");
// The select's empty "follow every import" option must clear to null too.
const read2 = (k) => (k === "trunk_branch" ? "" : "x");
const p3 = _deckMetaPayload(read2);
if (p3.trunk_branch !== null) fail(`blank trunk_branch must be null, got ${JSON.stringify(p3.trunk_branch)}`);
// A blank name would violate NOT NULL — it must be dropped, not nulled.
const p2 = _deckMetaPayload((k) => (k === "name" ? "   " : ""));
if (p2.name !== undefined && p2.name !== null) fail("blank name must not be sent as a value");
if (p2.name === null) fail("name is NOT NULL — must be omitted, not nulled");
"""
    )
    proc = subprocess.run([node, str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


# ---- the whack-a-mole guards ----


def test_every_helper_the_editor_calls_is_defined(js: str) -> None:
    """`node --check` validates syntax, not identifier existence.

    D19.22 shipped a call to a function that did not exist straight through
    a green check-web. Same scan, applied to this render path.
    """
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
    for fname in ("_renderDeckMetaEditor", "_deckMetaPayload", "_saveDeckMeta"):
        body = _fn(js, fname)
        body = re.sub(r"//[^\n]*", "", body)
        locals_ = set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", body))
        # A callable passed IN is defined by the caller. Without this the
        # scan flags `read()` in `_deckMetaPayload(read)` — a false
        # positive that would train someone to ignore the check.
        params = body[body.index("(") + 1 : body.index(")")]
        locals_ |= {p.strip() for p in params.split(",") if p.strip()}
        # Arrow-function parameters are callable too, e.g. `.map((f) => …)`.
        locals_ |= set(re.findall(r"\(\s*([A-Za-z_$][\w$]*)\s*\)\s*=>", body))
        for call in set(re.findall(r"(?<![.\w$])([a-zA-Z_$][\w$]*)\s*\(", body)):
            assert (
                call in builtins or call in defined or call in locals_
            ), f"{fname} calls undefined {call}()"


def test_boot_stays_the_last_top_level_statement(js: str) -> None:
    """D19.13: two TDZ bugs came from top-level code after the boot call."""
    tail = js[js.index("_bootFromUrl();") + len("_bootFromUrl();") :]
    assert not re.search(r"(?m)^[A-Za-z_$]", tail), "no top-level code after boot"


def test_confirmation_survives_the_reload(js: str) -> None:
    """`_loadDeck` re-renders the pane, destroying any status set before it.

    Setting "Saved." first makes a successful save look like it did
    nothing — caught in the browser, not by any static pin.
    """
    fn = _fn(js, "_saveDeckMeta")
    assert fn.index("_loadDeck(") < fn.index('"Saved."')


def test_panel_reopens_after_saving(js: str) -> None:
    """A <details> re-renders closed; the user would lose their place."""
    fn = _fn(js, "_saveDeckMeta")
    assert "open = true" in fn
