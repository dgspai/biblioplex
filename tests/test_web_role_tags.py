"""Colour-coded role tags (ramp / CA / TD / MD / plan) on cards."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "web"
CORE = (WEB / "js" / "00-core.js").read_text()
BUILD = (WEB / "js" / "90-build.js").read_text()
INV = (WEB / "js" / "50-inventory.js").read_text()
CSS = (WEB / "style.css").read_text()


def _slice(src: str, start: str) -> str:
    body = src[src.index(start) :]
    return body[: body.index("\n}\n") + 2]


def _role_tags(labels: list[str] | None) -> str:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node not available")
    pred = CORE[CORE.index("const PRED_LABELS") :]
    pred = pred[: pred.index(";\n") + 2]
    script = (
        pred
        + _slice(CORE, "const _ROLE_CODE").split("function roleTags", 1)[0]
        + _slice(CORE, "function roleTags(")
        + f"process.stdout.write(roleTags({json.dumps(labels)}));\n"
    )
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_tags_follow_canonical_order_with_short_codes() -> None:
    html = _role_tags(["plan", "ramp", "card_advantage"])
    assert html.index(">RAMP<") < html.index(">CA<") < html.index(">PLAN<")
    assert 'class="role-tag pred-card_advantage"' in html
    assert 'title="card advantage"' in html  # full name, not colour alone


def test_no_labels_renders_nothing() -> None:
    assert _role_tags([]) == ""
    assert _role_tags(None) == ""


def test_unknown_label_is_dropped() -> None:
    assert "bogus" not in _role_tags(["bogus", "ramp"])


def test_every_role_has_a_colour() -> None:
    for lab in ("ramp", "card_advantage", "targeted_disrupt", "mass_disrupt", "plan"):
        assert f".role-tag.pred-{lab}" in CSS


def test_tags_shown_in_inventory_and_build() -> None:
    assert "roleTags(r.predicted_labels)" in INV
    rows = _slice(BUILD, "function _buildCardRows(")
    assert rows.count("roleTags(c.predicted_labels)") == 2  # stack + list
    assert "roleTags(" in _slice(BUILD, "function _dlRow(")
    assert "tags" in _slice(BUILD, "function _stackCard(")
