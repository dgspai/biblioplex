"""Version-row note must not share a grid area with the meta line.

`.deck-version-msg` also carries `.upload-meta` (grid-area: meta), so the note
text overlapped the "N cards · M unique" line. Static pin over web/style.css.
"""

from __future__ import annotations

from pathlib import Path

CSS = (Path(__file__).resolve().parent.parent / "web" / "style.css").read_text()


def test_version_msg_has_its_own_grid_area() -> None:
    assert "grid-area: msg" in CSS[CSS.index(".deck-version-msg {") :][:120]
    assert '"msg actions"' in CSS
