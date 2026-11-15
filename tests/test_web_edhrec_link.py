"""Card detail header links out to the card's EDHREC page (no fetch)."""

from __future__ import annotations

from pathlib import Path

CORE = (Path(__file__).resolve().parent.parent / "web" / "js" / "00-core.js").read_text()


def _render_detail() -> str:
    body = CORE[CORE.index("function renderDetail(") :]
    return body[: body.index("\n}\n")]


def test_edhrec_link_uses_scryfall_related_uri() -> None:
    body = _render_detail()
    assert "card.related_uris?.edhrec" in body
    assert 'title="Open on EDHREC"' in body


def test_edhrec_link_sits_in_the_header_actions() -> None:
    header = _render_detail().split('<div class="detail-actions">', 1)[1]
    header = header.split("</div>", 1)[0]
    assert "${edhrecLink}" in header
