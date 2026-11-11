"""D19.15 — `.ext-link` is styled wherever it is used.

The rule was scoped to `.detail-pane .ext-link, #combosDetail .ext-link`,
so the anchors in `.upload-actions` (import history and deck version
downloads) matched nothing. There is no generic `a { }` rule in
style.css, so they fell back to the user-agent's default link blue —
unreadable against `--bg: #1a1a1a`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.web_assets import app_js

ROOT = Path(__file__).resolve().parent.parent
CSS = ROOT / "web" / "style.css"


@pytest.fixture(scope="module")
def css() -> str:
    return CSS.read_text()


def test_ext_link_rule_is_not_scoped_to_a_container(css: str) -> None:
    """Scoping bought nothing and silently excluded most call sites."""
    selectors = re.findall(r"^([^{}\n]*\.ext-link[^{}\n]*)\{", css, re.M)
    assert selectors, ".ext-link must still be styled"
    for sel in selectors:
        base = sel.split(",")[0].strip()
        assert base.startswith(".ext-link"), (
            f"scoped selector {base!r} leaves other .ext-link users unstyled"
        )


def test_ext_link_sets_an_explicit_colour(css: str) -> None:
    """Inheriting the UA default is the bug; a token is the fix."""
    # Anchor on the RULE, not the first textual mention — the comment
    # above it names the class while explaining why it is unscoped.
    match = re.search(r"(?m)^\.ext-link\s*\{([^}]*)\}", css)
    assert match, "no top-level .ext-link rule found"
    block = match.group(1)
    assert "color:" in block
    assert "var(--" in block, "use a theme token, not a literal colour"


def test_every_ext_link_user_is_covered(css: str) -> None:
    """More call sites than the two originally reported.

    Counting them here so a future scoping change has to confront how many
    places it would break.
    """
    users = app_js().count('class="ext-link"')
    assert users >= 6, f"expected the class to be widely used, found {users}"
