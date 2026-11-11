"""D19.22 — the deck renders as a card table, not an editable blob."""

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


# ---- read-only: the editor is gone ----


def test_decklist_textarea_is_gone(js: str) -> None:
    """Author decision: no in-place editing — versions come from imports.

    The textarea and its save control are removed from the Decks pane.
    """
    assert "deckListText" not in js


def test_save_as_new_version_control_is_gone(js: str) -> None:
    assert "deckSaveBtn" not in js
    assert "Save as new version" not in js


def test_the_api_patch_path_is_untouched(js: str) -> None:
    """Dropping a UI affordance is not deleting a capability.

    `PATCH /decks/{id}` with a decklist stays — it is shipped, tested,
    MCP-exposed surface that agents and curl callers use. The folder move
    still PATCHes, which is the proof the method was not ripped out.
    """
    assert '"PATCH"' in js


# ---- the table ----


def test_table_reads_the_cards_endpoint(js: str) -> None:
    assert "/cards`" in js or "/cards?" in js or "}/cards" in js


def test_sections_render_in_reading_order(js: str) -> None:
    """Commander first — a reader looks for it before the 99."""
    order = js[js.index("const _DECK_SECTION_ORDER") :]
    order = order[: order.index("\n")]
    ci = order.index("commander")
    mi = order.index("main")
    si = order.index("sideboard")
    assert ci < mi < si


def test_rows_carry_the_card_id_for_navigation(js: str) -> None:
    render = _fn(js, "_deckCardRow")
    assert "data-id" in render


def test_row_click_reuses_the_existing_card_detail_pipeline(js: str) -> None:
    """`selectCard()` already fetches /cards/:id and renders similar cards,
    oracle tags and predicted labels. Duplicating that would rot."""
    assert "selectCard(" in js
    handler = js[js.index("deckDetailEl.addEventListener") :]
    handler = handler[: handler.index("\n  });")]
    assert "deck-card-row" in handler
    assert "selectCard(" in handler


def test_unresolved_rows_render_but_are_not_clickable(js: str) -> None:
    """`resolved: false` rows have `id: null`. They still count toward the
    deck, so hiding them would make the table disagree with its own total —
    but a click would fetch /cards/null."""
    render = _fn(js, "_deckCardRow")
    assert "row.resolved" in render, "clickability must depend on `resolved`"
    assert "deck-card-unresolved" in render
    # The id attribute is emitted only for a resolved row, so a click can
    # never reach /cards/null.
    assert "clickable ?" in render


# ---- ownership: the EXISTING icon vocabulary ----


def test_ownership_reuses_the_inventory_and_wishlist_icons(js: str) -> None:
    """Author asked for the same icons as those tabs. `_ownedBadge` already
    renders the box / star SVG icons; inventing a second vocabulary would be
    inconsistent."""
    badge = _fn(js, "_deckOwnBadge")
    assert "ICON_BOX" in badge, "inventory box icon missing"
    assert "ICON_STAR" in badge, "wishlist star icon missing"


def test_ownership_is_quantity_aware(js: str) -> None:
    """Binary owned/not hides the gap on a partially-owned playset."""
    badge = _fn(js, "_deckOwnBadge")
    assert "qty_owned" in badge
    assert "qty" in badge


def test_partial_ownership_has_its_own_state(js: str) -> None:
    badge = _fn(js, "_deckOwnBadge")
    assert "partial" in badge


def test_partial_state_is_dimmed_like_the_wishlist_icon(css: str) -> None:
    """Follows the existing `.wanted-icon { filter: saturate(0.85) }`."""
    rules = re.findall(r"\.deck-own-partial[^{]*\{[^}]*\}", css)
    assert rules, "no .deck-own-partial rule"
    assert "filter" in " ".join(rules) or "opacity" in " ".join(rules)


def test_ownership_icons_are_not_colour_only(js: str) -> None:
    """The count carries the same information as the icon."""
    badge = _fn(js, "_deckOwnBadge")
    assert "title=" in badge or "aria-label" in badge


# ---- sorting ----


def test_table_is_sortable_including_by_ownership(js: str) -> None:
    sorter = _fn(js, "_deckCardSortValue")
    assert "qty_owned" in sorter
    assert "data-sort" in js


def test_sort_keeps_sections_together(js: str) -> None:
    """Sorting across sections would mix the commander into the 99."""
    sorter = _fn(js, "_sortedDeckCards")
    assert "section" in sorter


# ---- theme ----


def test_table_styles_use_theme_tokens(css: str) -> None:
    rules = re.findall(r"\.deck-card-table[^{]*\{[^}]*\}", css)
    assert rules
    joined = " ".join(rules)
    assert not re.search(r":\s*#[0-9a-fA-F]{3,6}\b", joined)


def test_mana_cost_uses_the_existing_symbol_renderer(js: str) -> None:
    """`node --check` validates syntax, not that an identifier exists.

    The first draft called `renderManaCost()`, which does not exist. The
    gate passed; the browser showed "Cards unavailable: renderManaCost is
    not defined" and an empty table. This pins the real renderer's name.
    """
    row = _fn(js, "_deckCardRow")
    assert "renderSymbols(" in row
    assert "renderManaCost" not in js


def test_every_helper_the_table_calls_is_defined(js: str) -> None:
    """Cheap guard for the whole class of bug above.

    Any bare `name(` called from the table's render path must have a
    matching `function name(` somewhere in the file.
    """
    defined = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", js))
    # Module-level arrow/function consts count as defined too (`$`, `api`, …).
    defined |= set(re.findall(r"^(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:\(|async|function)", js, re.M))
    builtins = {
        "if", "for", "while", "switch", "catch", "return", "typeof",
        "String", "Number", "Array", "Object", "Set", "Map", "Promise",
        "JSON", "parseInt", "parseFloat", "encodeURIComponent", "fetch",
        "setTimeout", "console", "isNaN",
    }
    for fname in ("_deckCardRow", "_deckOwnBadge", "_renderDeckCardTable", "_sortedDeckCards"):
        body = _fn(js, fname)
        # Strip line comments first — prose like "the Wishlist tabs (…)"
        # otherwise reads as a call to `tabs()`.
        body = re.sub(r"//[^\n]*", "", body)
        # Locals declared inside the function are legitimate callees too.
        locals_ = set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", body))
        for call in set(re.findall(r"(?<![.\w$])([a-zA-Z_$][\w$]*)\s*\(", body)):
            if call in builtins or call in defined or call in locals_ or call.startswith("_DECK"):
                continue
            # locals, methods and constructors are filtered by the lookbehind;
            # anything left must be a module-level function.
            assert call in defined, f"{fname} calls undefined {call}()"
