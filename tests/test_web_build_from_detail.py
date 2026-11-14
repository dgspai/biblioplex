"""Card detail → Build: add a card to the in-progress build, or start one."""

from __future__ import annotations

from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
CORE = (WEB / "js" / "00-core.js").read_text()
BUILD = (WEB / "js" / "90-build.js").read_text()


def _fn(src: str, name: str) -> str:
    body = src[src.index(f"function {name}(") :]
    return body[: body.index("\n}\n")]


def test_detail_header_carries_the_build_actions() -> None:
    assert "buildDetailActions(card)" in _fn(CORE, "renderDetail")


def test_add_button_states() -> None:
    body = _fn(BUILD, "buildDetailActions")
    assert "data-build-detail-add=" in body
    assert "_buildHas(" in body and "in build" in body
    assert "inIdentity(" in body and "disabled" in body  # off-colour can't be added


def test_start_deck_only_for_commanders() -> None:
    body = _fn(BUILD, "buildDetailActions")
    assert "card.is_commander" in body and "data-build-start=" in body


def test_start_replaces_after_confirm_and_opens_build() -> None:
    body = _fn(BUILD, "_startBuildFrom")
    assert "_detailCard" not in body  # a stale pane must not start the wrong card
    assert "window.confirm(" in body and "_resetBuild()" in body
    assert "_build.step = 2" in body and '_setTab("build")' in body


def test_one_reset_path() -> None:
    reset = BUILD[BUILD.index('e.target.closest("#buildReset")') :][:300]
    assert "_resetBuild()" in reset
