"""D19.44 — the Decks tab's complete / missing-cards filter."""

from __future__ import annotations

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
    return js[start : js.index("\n}\n", start) + 3]


def test_control_exists_with_the_three_states(html: str) -> None:
    assert 'id="deckCompletion"' in html
    block = html[html.index('id="deckCompletion"') : html.index("</select>", html.index('id="deckCompletion"'))]
    for value in ('value=""', 'value="incomplete"', 'value="complete"'):
        assert value in block, f"missing {value}"


def test_filtering_goes_through_the_api(js: str) -> None:
    """Not a client-side hide.

    The listing is paginated, so filtering the rows already on screen would
    leave matching decks stranded on a later page — the same reason D19.25
    sends the sort to the server instead of reordering locally.
    """
    assert "_deckCompletionQuery()" in _fn(js, "refreshDecksList")
    assert "completion=" in _fn(js, "_deckCompletionQuery")


def test_change_refreshes_the_list(js: str) -> None:
    listener = js[js.index("if (deckCompletionEl) {") :][:220]
    assert "addEventListener" in listener and "refreshDecksList()" in listener


def test_empty_selection_omits_the_parameter(tmp_path: Path, js: str) -> None:
    """Executed, not substring-matched.

    `?completion=` (present but empty) is a value the server whitelist
    rejects with a 422, while omitting the parameter means "all decks".
    A static pin cannot tell those two apart, and flattening them is exactly
    what a later refactor does.
    """
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node not available")
    script = tmp_path / "q.js"
    script.write_text(
        "let value = '';\n"
        "const deckCompletionEl = { get value() { return value; } };\n"
        + _fn(js, "_deckCompletionQuery")
        + """
const fail = (m) => { console.error(m); process.exit(1); };
if (_deckCompletionQuery() !== "") fail(`"all decks" must send nothing, got ${JSON.stringify(_deckCompletionQuery())}`);
value = "incomplete";
if (_deckCompletionQuery() !== "&completion=incomplete") fail(`got ${JSON.stringify(_deckCompletionQuery())}`);
value = "complete";
if (_deckCompletionQuery() !== "&completion=complete") fail(`got ${JSON.stringify(_deckCompletionQuery())}`);
"""
    )
    proc = subprocess.run([node, str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_filter_and_sort_compose(js: str) -> None:
    """Both must reach the URL — filtering is not sorting."""
    fn = _fn(js, "refreshDecksList")
    assert "_deckSortQuery()" in fn and "_deckCompletionQuery()" in fn
    assert "_deckFolderQuery()" in fn, "the folder filter must survive too"


def test_no_undefined_calls_in_the_filter_path(js: str) -> None:
    """`node --check` validates syntax, not identifier existence (D19.22)."""
    fn = _fn(js, "_deckCompletionQuery")
    called = {c.split("(")[0].strip() for c in fn.split() if c.endswith("(")}
    for name in called:
        if name.isidentifier() and not name.startswith(("if", "return", "for")):
            assert f"function {name}(" in js or f"const {name}" in js or name in (
                "encodeURIComponent",
            ), f"{name} is called but never defined"
