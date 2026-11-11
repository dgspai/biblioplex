"""Cards screen: no helper text in the bar, pager only when it pages."""

from __future__ import annotations

import re
from pathlib import Path

from tests.web_assets import app_js

INDEX = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text()


def test_query_placeholder_is_a_label_not_a_tutorial() -> None:
    tag = re.search(r'<input id="q"[^>]*>', INDEX).group(0)
    assert 'placeholder="Search cards"' in tag


def test_pager_hides_when_everything_fits_on_one_page() -> None:
    js = app_js()
    body = js[js.index("function _updateCardsPager("):]
    body = body[: body.index("\n}\n")]
    assert "total <= SEARCH_PAGE_SIZE" in body
    assert "page ${page} /" not in body
