"""Build tab: search options (checkboxes + sort) persist across sessions."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = (Path(__file__).resolve().parent.parent / "web" / "js" / "90-build.js").read_text()


def _fn(name: str) -> str:
    body = JS[JS.index(f"function {name}(") :]
    return body[: body.index("\n}\n") + 2]


def _const(name: str) -> str:
    body = JS[JS.index(f"const {name} ") :]
    return body[: body.index(";\n") + 2]


def _load_prefs(stored: str | None) -> dict:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node not available")
    script = (
        f"const _stored = {json.dumps(stored)};\n"
        "global.localStorage = {getItem: () => _stored};\n"
        + _const("BUILD_PREFS_KEY")
        + _const("_BUILD_PREF_DEFAULTS")
        + _fn("_loadBuildPrefs")
        + "console.log(JSON.stringify(_loadBuildPrefs()));\n"
    )
    proc = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_fresh_profile_gets_the_old_defaults() -> None:
    p = _load_prefs(None)
    assert p["bsLegal"] is True and p["bsSlot"] is True and p["bsHide"] is True
    assert p["bsOwned"] is False and p["bsSort"] == "edhrec"


def test_stored_choices_override_defaults() -> None:
    p = _load_prefs(json.dumps({"bsOwned": True, "bsSort": "usd"}))
    assert p["bsOwned"] is True and p["bsSort"] == "usd"
    assert p["bsLegal"] is True  # unstored key keeps its default


def test_corrupt_storage_falls_back_to_defaults() -> None:
    assert _load_prefs("{not json")["bsOwned"] is False


def test_search_panel_renders_from_prefs_not_hardcoded() -> None:
    body = _fn("_buildSearchPanelHtml")
    assert "_buildPrefs" in body
    assert " checked>" not in body  # no hardcoded checked state left


def test_changing_an_option_saves_it() -> None:
    body = JS[JS.index('addEventListener("change"') :]
    body = body[: body.index("\n});\n")]
    assert "_BUILD_PREF_DEFAULTS" in body and "_saveBuildPrefs()" in body
