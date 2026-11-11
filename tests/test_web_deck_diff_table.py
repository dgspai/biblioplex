"""D19.23 — version compare renders as a PR-style diff table."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.web_assets import app_js

ROOT = Path(__file__).resolve().parent.parent
CSS = ROOT / "web" / "style.css"


@pytest.fixture(scope="module")
def js() -> str:
    return app_js()


@pytest.fixture(scope="module")
def css() -> str:
    return CSS.read_text()


def _fn(js: str, name: str) -> str:
    start = js.index(f"function {name}(")
    return js[start : js.index("\n}\n", start)]


def test_diff_renders_a_table(js: str) -> None:
    render = _fn(js, "_renderDeckDiff")
    assert "deck-diff-table" in render


def test_removed_and_added_have_distinct_row_classes(js: str) -> None:
    render = _fn(js, "_renderDeckDiff")
    # Built as `deck-diff-${kind}`, so pin the template plus both kinds.
    assert "deck-diff-${kind}" in render
    assert '"del"' in render and '"add"' in render


def test_sign_prefix_carries_the_meaning_without_colour(js: str) -> None:
    """Colour must not be the only signal.

    Roughly 1 in 12 men cannot reliably separate red from green; a diff
    that encodes add/remove only in colour is unreadable to them.
    """
    render = _fn(js, "_renderDeckDiff")
    assert '"+"' in render or "'+'" in render or "+${" in render
    assert "−" in render or "-${" in render


def test_quantity_change_shows_both_values(js: str) -> None:
    """`3 → 1` is the information; a bare delta hides the starting point."""
    render = _fn(js, "_renderDeckDiff")
    assert "from" in render and "to" in render


def test_quantity_change_renders_as_a_paired_removal_and_addition(js: str) -> None:
    """A PR diff shows a modified line as `-old` then `+new`."""
    render = _fn(js, "_renderDeckDiff")
    body = render[render.index("qty_changed") :]
    assert '"del"' in body and '"add"' in body, "qty change must emit both halves"
    assert "x.from" in body and "x.to" in body


def test_identical_versions_say_so(js: str) -> None:
    render = _fn(js, "_renderDeckDiff")
    assert "Identical" in render


def test_diff_colours_are_theme_aware(css: str) -> None:
    """The palette defines dark-mode overrides; a single hardcoded pair
    would be unreadable in one of the two themes."""
    light = re.findall(r"\.deck-diff-(?:add|del)\b[^{]*\{[^}]*\}", css)
    assert light, "no diff row rules"
    # Rules use tokens; each token must be redefined in the dark theme blocks.
    assert "var(--diff-add-bg)" in css and "var(--diff-del-bg)" in css
    for block in re.findall(r':root:not\(\[data-theme="light"\]\) \{[^}]*\}|:root\[data-theme="dark"\] \{[^}]*\}', css):
        for tok in ("--diff-add-bg", "--diff-add-fg", "--diff-del-bg", "--diff-del-fg"):
            assert tok in block, f"{tok} missing from a dark block"


def test_diff_row_colours_do_not_rely_on_the_default_text_colour(css: str) -> None:
    rules = " ".join(re.findall(r"\.deck-diff-(?:add|del)\b[^{]*\{[^}]*\}", css))
    assert "background" in rules
