"""Build tab: List/Stack toggle on the decklist and recommendations."""

from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
JS = (WEB / "js" / "90-build.js").read_text()
CSS = (WEB / "style.css").read_text()


def _fn(name: str) -> str:
    body = JS[JS.index(f"function {name}(") :]
    return body[: body.index("\n}\n")]


def test_views_default_to_list_and_persist() -> None:
    defaults = JS[JS.index("const _BUILD_PREF_DEFAULTS") :].split(";\n", 1)[0]
    assert 'dlView: "list"' in defaults and 'recView: "list"' in defaults
    click = JS[JS.index("[data-build-view]") :][:600]
    assert "_saveBuildPrefs()" in click


def test_both_panels_offer_the_toggle() -> None:
    assert '_viewToggle("dlView")' in _fn("_renderDeckPanel")
    assert '_viewToggle("recView")' in _fn("_buildSearchPanelHtml")


def test_stack_cards_show_the_image_and_keep_controls() -> None:
    card = _fn("_stackCard")
    assert "<img" in card and 'loading="lazy"' in card
    assert 'tabindex="0"' in card  # tap/keyboard reveals the card: no hover needed
    assert "data-no-hover" in card  # card itself is the preview
    recs = _fn("_buildCardRows")
    assert "_stackCard(" in recs and "data-build-add" in _fn("_addBtn")
    assert "_stackCard(" in _fn("_dlRow") and "data-build-basic-minus" in _fn("_dlRow")


def test_recommendations_fetch_images() -> None:
    proj = JS[JS.index("const _BUILD_LIST_PROJ") :].split(";\n", 1)[0]
    assert "image_uris.normal" in proj and "card_faces.0.image_uris.normal" in proj


def test_stack_cards_can_still_be_dragged_and_added() -> None:
    assert ".build-stack-card[draggable]" in JS
    assert 'add.closest("[data-id]")' in JS


def test_decklist_shows_on_mobile() -> None:
    assert not re.search(r"\.build-decklist\s*\{\s*display:\s*none", CSS)


def test_reveal_works_without_hover() -> None:
    assert ".build-stack-card:focus-within ~ .build-stack-card" in CSS


def test_phone_shows_one_pile_per_row() -> None:
    """Piles side by side overflow a phone; stack them in one column."""
    phone = CSS[CSS.index("@media (max-width: 680px) {\n  .build-stacks") :]
    phone = phone[: phone.index("\n}\n")]
    assert "flex-direction: column" in phone
    assert "--card-w: min(" in phone  # fills the width, capped
