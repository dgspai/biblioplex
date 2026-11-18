"""Land profile in the UI: labels, the land build step, and the deck manabase line."""

from __future__ import annotations

from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
CORE = (WEB / "js" / "00-core.js").read_text()
BUILD = (WEB / "js" / "90-build.js").read_text()
DECKS = (WEB / "js" / "80-decks.js").read_text()


def _fn(src: str, name: str) -> str:
    body = src[src.index(f"function {name}(") :]
    return body[: body.index("\n}\n")]


def test_land_tags_are_visible_text() -> None:
    body = _fn(CORE, "landTags")
    assert "land-tag" in body and "title=" not in body  # no tooltip-only info


def test_card_detail_shows_land_tags() -> None:
    assert "landTags(card.land_profile)" in _fn(CORE, "renderDetail")


def test_build_rows_fetch_and_show_land_profile() -> None:
    assert "land_profile" in BUILD[BUILD.index("const _BUILD_LIST_PROJ") :][:200]
    rows = _fn(BUILD, "_buildCardRows")
    assert rows.count("landTags(") >= 2  # list and stack views


def test_land_step_reuses_the_recommendations_panel() -> None:
    step = _fn(BUILD, "_renderBuildStep6")
    assert '_buildSearchPanelHtml("t:land", true)' in step
    assert "_landFacetChipsHtml()" in step
    assert "_dlTypeGroups(" in step  # cut list: same list/stack rows as the deck
    assert "_buildSwapRows()" in step  # nonbasic swaps: same rows with labels
    assert "_buildCardRows(" in _fn(BUILD, "_buildSwapRows")


def test_land_facets_filter_the_search() -> None:
    assert "_landFacetQuery()" in _fn(BUILD, "_runBuildSearch")
    assert "land:${" in _fn(BUILD, "_landFacetQuery")
    assert "data-land-facet=" in _fn(BUILD, "_landFacetChipsHtml")


def test_land_step_goal_line_uses_the_manabase_summary() -> None:
    assert "a.lands" in _fn(BUILD, "_renderBuildStep6")


def test_deck_stats_show_manabase() -> None:
    assert "_renderLandSummary(a.lands)" in _fn(DECKS, "_renderDeckStats")


def test_rows_with_role_tags_do_not_repeat_roles_as_land_chips() -> None:
    assert "landTags(c.land_profile, false)" in _fn(BUILD, "_buildCardRows")


def test_land_step_groups_lands_by_colours_like_card_types() -> None:
    assert "_LAND_GROUPS" in BUILD[BUILD.index("const _DECK_TYPE_ORDER") :][:300]
    bucket = _fn(BUILD, "_landBucket")
    assert "_LAND_STEP" in bucket and ".colors" in bucket and ".etb" not in bucket
    for label in ("5-color", "2-color", "Colorless", "No-mana"):
        assert f'"{label}' in BUILD
    assert "_landBucket(" in _fn(BUILD, "_dlTypeGroups")
    assert "_landBucket(" in _fn(BUILD, "_buildCardRows")


def test_fetch_lands_get_their_own_group_and_label() -> None:
    assert "fetch:" in CORE[CORE.index("const _LAND_UTIL_LABEL") :][:200]
    bucket = _fn(BUILD, "_landBucket")
    assert '"fetch"' in bucket and "Fetch lands" in BUILD
    order = BUILD[BUILD.index("const _LAND_GROUPS") :][:200]
    assert '["Fetch", ...' in order  # fetches lead the land groups


def test_land_facets_offer_fetch() -> None:
    assert "fetch" in BUILD[BUILD.index("const _LAND_FACETS") :][:800]
