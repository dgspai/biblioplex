"""Settings screen: grouped rows, no explanatory paragraphs."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = (ROOT / "web" / "index.html").read_text()
CSS = (ROOT / "web" / "style.css").read_text()


def _pane() -> str:
    start = INDEX.index('id="settingsPane"')
    return INDEX[start : INDEX.index("<footer", start)]


def test_no_help_paragraphs() -> None:
    assert '<p class="help">' not in _pane()


def test_no_colon_labels_or_dependency_tags() -> None:
    pane = _pane()
    assert "CSV file:" not in pane
    assert "refresh-menu-dep" not in pane


def test_dependencies_survive_as_tooltips() -> None:
    """The auto-check enforces them; the hint moves to hover, not away."""
    pane = _pane()
    stages = pane[pane.index('class="refresh-stages"') :]
    stages = stages[: stages.index("</div>")]
    assert stages.count("title=\"Needs") >= 5


def test_sections_are_groups_not_a_card_grid() -> None:
    pane = _pane()
    assert "settings-grid" not in pane and "settings-card" not in pane
    assert pane.count('class="settings-group"') == 3


def test_status_lines_sit_inside_their_form() -> None:
    """Each import reports beside the row that triggered it."""
    pane = _pane()
    for form, status in (("inventorySyncForm", "invSyncStatus"), ("wishlistSyncForm", "wlSyncStatus")):
        block = pane[pane.index(f'id="{form}"') :]
        block = block[: block.index("</form>")]
        assert f'id="{status}"' in block, form


def test_sub_headings_are_not_shouted() -> None:
    assert "text-transform: uppercase" not in CSS[CSS.index("Settings tab") : CSS.index("Decks tab")]
