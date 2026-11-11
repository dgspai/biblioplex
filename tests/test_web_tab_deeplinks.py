"""D19.13 — every tab is reachable by `?tab=`, and deep-links load data.

Static pins over web/js/*.js. app.js has no JS unit harness (only
helpers.js does, via web/test_helpers.html), so these guard the structure
that makes the behaviour correct; the behaviour itself was confirmed in a
headless browser and recorded in the commit body.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.web_assets import app_js

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "web" / "index.html"


@pytest.fixture(scope="module")
def js() -> str:
    return app_js()


@pytest.fixture(scope="module")
def tabs_in_markup() -> list[str]:
    return re.findall(r'data-tab="([a-z]+)"', INDEX.read_text())


def test_markup_declares_eight_tabs(tabs_in_markup: list[str]) -> None:
    assert len(tabs_in_markup) == 8
    assert "build" in tabs_in_markup
    assert "deck" not in tabs_in_markup  # standalone Decklist tab removed


def test_every_tab_has_a_click_handler(js: str, tabs_in_markup: list[str]) -> None:
    """Each nav button must wire ``_setTab("<tab>")``.

    Click wiring is manual per button and split across two blocks, so a tab
    added to the markup + `_setTab` is easy to leave unclickable — the Build
    tab shipped exactly that bug (deep-link worked, the nav button did not).
    Boot uses `_setTab(tab)` with a variable, so requiring the quoted form
    specifically tests the click handler, not the boot dispatch.
    """
    for tab in tabs_in_markup:
        assert f'_setTab("{tab}")' in js, tab


def test_boot_does_not_hardcode_a_tab_allowlist(js: str) -> None:
    """The old ternary chain accepted deck/compare/combos and silently
    dropped the other five onto Cards — including on a plain refresh."""
    boot = js[js.index("function _bootFromUrl()"):]
    boot = boot[: boot.index("\n}")]
    assert 'tabParam === "deck" ? "deck"' not in boot
    assert 'tabParam === "compare"' not in boot


def test_boot_accepts_every_tab_in_the_markup(js: str, tabs_in_markup: list[str]) -> None:
    """Derived from the registry, so a future tab works by construction."""
    boot = js[js.index("function _bootFromUrl()"):]
    boot = boot[: boot.index("\n}")]
    assert "_TAB_NAMES" in boot


def test_tab_registry_is_derived_from_the_dom(js: str) -> None:
    assert "data-tab" in js, "the registry must come from the markup"
    assert "_TAB_NAMES" in js


def test_set_tab_dispatches_the_data_loader(js: str) -> None:
    """`_setTab` alone used to leave a tab empty.

    `refreshDecksList()` hung off the nav click handler, so a deep-link —
    or anything calling `_setTab` directly — rendered an empty Decks tab.
    That cost a false bug report during the D19.12 browser pass.
    """
    set_tab = js[js.index("function _setTab("):]
    set_tab = set_tab[: set_tab.index("\n}\n")]
    assert "_TAB_LOADERS" in set_tab


def test_loader_map_covers_every_tab_that_fetches(js: str) -> None:
    loaders = js[js.index("const _TAB_LOADERS"):]
    loaders = loaders[: loaders.index("};")]
    for tab in ("decks", "inventory", "wishlist", "settings", "combos"):
        assert f"{tab}:" in loaders, tab


def test_boot_runs_after_all_module_initialisation(js: str) -> None:
    """`_bootFromUrl()` must be the LAST top-level statement.

    `_setTab` now dispatches each tab's data loader, so booting before the
    module's `let`s have initialised makes a deep-link call into the
    temporal dead zone. `?tab=inventory` died on `Cannot access '_invPage'
    before initialization` — caught only by reading the browser console,
    because the DOM looked correct. Hoisting each offending variable is
    whack-a-mole; booting last removes the class.
    """
    call = js.index("\n_bootFromUrl();")
    after = js[call + len("\n_bootFromUrl();"):]
    remaining = [
        line for line in after.splitlines()
        if line.strip() and not line.startswith(("//", " ", "\t"))
    ]
    assert not remaining, f"top-level code runs after boot: {remaining[:3]}"


def test_boot_time_guards_are_declared_before_boot_runs(js: str) -> None:
    """Belt and braces alongside boot-last: the loaded-flags are grouped."""
    boot_call = js.index("\n_bootFromUrl();")
    for guard in ("_invLoaded", "_wlLoaded", "_combosLoaded"):
        assert js.index(f"let {guard}") < boot_call, guard


def test_search_input_is_not_spellchecked_and_esc_closes_dropdown_only(js: str) -> None:
    """F8/F27: DSL tokens must not get red squiggles, and Esc over an open
    autocomplete/history dropdown must not wipe the query (native type=search
    behaviour) — the handler preventDefaults and stops the document-level clear."""
    qtag = re.search(r'<input id="q"[^>]*>', INDEX.read_text()).group(0)
    assert 'spellcheck="false"' in qtag and 'autocapitalize="off"' in qtag
    for opener in ('hideAutocomplete();\n  } else if (e.key === "Tab"', "hideHistory();\n  } else if (e.key === \"Backspace\""):
        head = js[: js.index(opener)]
        branch = head[head.rindex('e.key === "Escape"'):]
        assert "e.preventDefault()" in branch and "e.stopPropagation()" in branch


def test_boot_reads_url_state_before_set_tab(js: str) -> None:
    """`_setTab` ends in `_syncUrl()`, which serialises the (still empty)
    search box into the URL. Boot used to read `?q=` only after that, so a
    shared `?q=t:goblin` link loaded DEFAULT_Q instead."""
    boot = js[js.index("function _bootFromUrl()"):]
    boot = boot[: boot.index("\n}")]
    assert boot.index("urlStateFromSearch(location.search)") < boot.index("_setTab(tab)")


def test_sync_url_keeps_tab_for_every_non_search_tab(js: str) -> None:
    """Only compare/combos wrote `?tab=`; Decks/Settings/etc rewrote the URL
    to the card-search state, so a refresh landed back on Cards."""
    sync = js[js.index("function _syncUrl()"):]
    sync = sync[: sync.index("\n}")]
    assert '"?tab=" + _currentTab' in sync


def test_combos_deeplink_hydrates_form_before_set_tab(js: str) -> None:
    """`_setTab("combos")` runs the combos loader, which searches with whatever
    the form holds. Hydrating AFTER `_setTab` fired an unfiltered search that
    raced the filtered one, so `?tab=combos&card=X` showed unfiltered results
    until the user pressed Search."""
    boot = js[js.index("function _bootFromUrl()"):]
    boot = boot[: boot.index("\n}")]
    assert boot.index("_combosStateFromUrl(params)") < boot.index("_setTab(tab)")
    # The loader is the single search; boot must not fire a second one.
    assert "runCombosSearch()" not in boot
