"""D19.25 — the deck library exposes D19.21's coverage sort."""

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


def test_sort_control_exists_in_the_markup(html: str) -> None:
    assert 'id="deckSort"' in html


def test_all_three_server_sort_keys_are_offered(html: str) -> None:
    block = html[html.index('id="deckSort"') :]
    block = block[: block.index("</select>")]
    for key in ("updated", "name", "coverage"):
        assert f'value="{key}"' in block, key


def test_both_directions_are_offered(html: str) -> None:
    """Descending = "what can I build"; ascending = "what must I buy"."""
    block = html[html.index('id="deckSort"') :]
    block = block[: block.index("</select>")]
    assert "coverage" in block
    assert block.count('value="coverage') >= 2 or 'id="deckSortDir"' in html


def test_sort_goes_through_the_api_not_a_client_reorder(js: str) -> None:
    """The list is paginated. Reordering the current page would leave the
    top-coverage deck invisible on page 2."""
    fn = _fn(js, "_deckSortQuery")
    assert "sort=" in fn
    assert "direction=" in fn
    refresh = _fn(js, "refreshDecksList")
    assert "_deckSortQuery()" in refresh


def test_coverage_is_shown_on_each_row(js: str) -> None:
    """Sorting by a number the reader cannot see is not usable."""
    render = _fn(js, "_renderDecksList")
    assert "_deckCoverageLabel(" in render, "row does not show its sort key"


def test_null_coverage_renders_as_unknown_not_zero(js: str) -> None:
    """`coverage_pct` is null for a deck the derived index has not covered.

    Rendering that as 0% claims you own none of it, which is a different
    and wrong statement.
    """
    fn = _fn(js, "_deckCoverageLabel")
    assert "=== null" in fn, "null must be handled explicitly, not falsily"
    # Strip comments: the rationale prose legitimately mentions 0%.
    code = re.sub(r"//[^\n]*", "", fn)
    assert "0%" not in code, "null coverage must not render as 0%"


def test_default_sort_is_unchanged(js: str) -> None:
    """Existing behaviour must not shift under anyone."""
    assert '_deckSort = { key: "updated"' in js or '"updated"' in _fn(js, "_deckSortQuery")


def test_coverage_label_executes_correctly(js: str, tmp_path: Path) -> None:
    """Run the null-vs-zero branch for real — the distinction is the whole
    point of the row and a substring pin would not catch an inversion."""
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node not available")
    fn = _fn(js, "_deckCoverageLabel")
    fn = js[js.index("function _deckCoverageLabel(") :]
    fn = fn[: fn.index("\n}\n") + 3]
    script = tmp_path / "cov.js"
    script.write_text(
        fn
        + """
const cases = [
  [{coverage_pct: null, owned_cards: 0}, /unknown/i],
  [{coverage_pct: 0, owned_cards: 0},    /0%/],
  [{coverage_pct: 56, owned_cards: 56},  /56%/],
];
for (const [row, want] of cases) {
  const got = _deckCoverageLabel(row);
  if (!want.test(got)) {
    console.error(`row=${JSON.stringify(row)} got ${JSON.stringify(got)} want ${want}`);
    process.exit(1);
  }
}
"""
    )
    proc = subprocess.run([node, str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
