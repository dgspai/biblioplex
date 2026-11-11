"""Phone layout: bottom tab bar, one pane at a time, no focus zoom.

Static pins over web/index.html, web/js/*.js and web/style.css.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.web_assets import app_js

ROOT = Path(__file__).resolve().parent.parent
INDEX = (ROOT / "web" / "index.html").read_text()
CSS = (ROOT / "web" / "style.css").read_text()


def _phone_css() -> str:
    start = CSS.index("@media (max-width: 768px)")
    depth, i = 0, CSS.index("{", start)
    while True:
        depth += {"{": 1, "}": -1}.get(CSS[i], 0)
        if depth == 0:
            return CSS[start:i]
        i += 1


def test_viewport_reaches_the_home_indicator() -> None:
    assert "viewport-fit=cover" in INDEX
    assert "safe-area-inset-bottom" in _phone_css()


def test_sidebar_becomes_a_bottom_tab_bar() -> None:
    phone = _phone_css()
    assert re.search(r"\.sidebar\s*\{[^}]*grid-row:\s*2", phone)
    assert re.search(r"\.sidebar-foot\s*\{[^}]*display:\s*contents", phone)


def test_inputs_are_16px_so_ios_does_not_zoom() -> None:
    assert re.search(r"input, select, textarea\s*\{[^}]*font-size:\s*16px", _phone_css())


def test_every_detail_pane_opens_over_its_list() -> None:
    js = app_js()
    assert '".cards-split > .detail-pane, #deckDetail"' in js
    assert "detail-open" in js and "split-back" in js
    assert ".detail-open" in _phone_css()
    assert len(re.findall(r'class="split-pane detail-pane"', INDEX)) == 4
