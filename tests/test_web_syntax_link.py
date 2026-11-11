"""The search bar has ONE syntax link: Biblioplex's DSL docs.

Those docs link Scryfall's guide in their first paragraph, so a second
"(Scryfall)" link in the bar was a secondary label with no new destination.
"""

from __future__ import annotations

import re
from pathlib import Path

INDEX = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text()
DOCS = "https://github.com/dgspai/biblioplex/blob/main/docs/search-syntax.md"


def test_search_syntax_link_points_at_biblioplex_docs() -> None:
    m = re.search(r'<a href="([^"]+)"[^>]*aria-label="Search syntax"', INDEX)
    assert m and m.group(1) == DOCS


def test_no_second_syntax_link_in_the_bar() -> None:
    assert 'href="https://scryfall.com/docs/syntax"' not in INDEX
