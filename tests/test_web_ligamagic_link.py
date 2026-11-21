"""Card detail header links out to a LigaMagic name search (no fetch)."""

from __future__ import annotations

from pathlib import Path

CORE = (Path(__file__).resolve().parent.parent / "web" / "js" / "00-core.js").read_text()


def _render_detail() -> str:
    body = CORE[CORE.index("function renderDetail(") :]
    return body[: body.index("\n}\n")]


def test_ligamagic_link_searches_front_face_name() -> None:
    body = _render_detail()
    assert "https://www.ligamagic.com.br/?view=cards%2Fsearch&tipo=1&card=" in body
    assert 'split(" // ")[0]' in body
    assert 'title="Search on LigaMagic"' in body


def test_ligamagic_link_sits_in_the_header_actions() -> None:
    header = _render_detail().split('<div class="detail-actions">', 1)[1]
    header = header.split("</div>", 1)[0]
    assert "${ligaLink}" in header
