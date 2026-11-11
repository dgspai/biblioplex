"""D19.39 — the detail header's commander/companion follow the version
being VIEWED, not head.

D19.35's branch-head navigator swaps the card table for another branch's
own latest version but left the header rendering head's
`effective_commander` / `effective_companion` — so viewing a branch whose
commander differs showed the new card list beside the old commander.
`_deckCommanderCompanionHtml` is the pure formatter; `_updateDeckHeaderCommander`
targets it into the DOM without re-rendering the whole detail pane (which
would also blow away the meta-editor / import panel's open state).
"""

from __future__ import annotations

import re
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


# ---- markup wiring ----


def test_header_wraps_commander_companion_in_a_targetable_span(js: str) -> None:
    body = _fn(js, "_renderDeckDetail")
    assert 'id="deckHeaderCC"' in body
    assert "_deckCommanderCompanionHtml(deck, deck.commander, deck.companion)" in body


def test_load_deck_cards_updates_header_from_the_viewed_version(js: str) -> None:
    body = _fn(js, "_loadDeckCards")
    assert "_updateDeckHeaderCommander(body.commander, body.companion)" in body
    assert "_updateDeckHeaderCommander(_deckDetailData.commander, _deckDetailData.companion)" in body


def test_update_helper_targets_the_header_span_not_a_full_rerender(js: str) -> None:
    body = _fn(js, "_updateDeckHeaderCommander")
    assert '$("#deckHeaderCC")' in body
    assert "_renderDeckDetail" not in body


# ---- pure logic, node-exercised ----


def test_commander_companion_html(js: str, tmp_path: Path) -> None:
    node = None
    import shutil

    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node not available")
    start = js.index("function _deckCommanderCompanionHtml(")
    fn = js[start : js.index("\n}\n", start) + 3]
    script = tmp_path / "commander_companion.js"
    script.write_text(
        f"""
const escapeHtml = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
{fn}
const fail = (m) => {{ console.error(m); process.exit(1); }};

// Derived commander, no override: plain text, no "(fixed)" marker.
const derived = _deckCommanderCompanionHtml({{}}, "Atraxa, Praetors' Voice", null);
if (!derived.includes("Atraxa")) fail("derived commander missing: " + derived);
if (derived.includes("fixed")) fail("a derived commander must not read as fixed: " + derived);

// Override present: the same text is marked, so it reads as deliberate.
const overridden = _deckCommanderCompanionHtml({{ commander_override: "The Ur-Dragon" }}, "The Ur-Dragon", null);
if (!overridden.includes("fixed")) fail("an overridden commander must be visibly marked: " + overridden);

// No commander at all -> nothing rendered for that half.
const none = _deckCommanderCompanionHtml({{}}, null, null);
if (none.trim() !== "") fail("no commander/companion must render nothing: " + JSON.stringify(none));

// Companion follows the same override rule, independently of commander.
const companionOverride = _deckCommanderCompanionHtml(
  {{ companion_override: "Lurrus of the Dream-Den" }}, null, "Lurrus of the Dream-Den"
);
if (!companionOverride.includes("companion")) fail("companion label missing: " + companionOverride);
if (!companionOverride.includes("fixed")) fail("an overridden companion must be visibly marked: " + companionOverride);
"""
    )
    proc = subprocess.run([node, str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


# ---- the whack-a-mole guard (D19.22's lesson, again) ----


def test_every_helper_this_deliverable_touches_is_defined(js: str) -> None:
    """`node --check` validates syntax, not identifier existence — same
    scan as D19.35's, extended to the two new/changed functions."""
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
        "_deckCommanderCompanionHtml",
        "_updateDeckHeaderCommander",
        "_renderDeckDetail",
        "_loadDeckCards",
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
