"""A saved pane split must not crash boot.

`_applySplit(saved)` ran before `const _splitGrids` was declared, so any
browser that had ever dragged a splitter threw a TDZ ReferenceError at load
and every handler wired after it (random card, combos, ...) was dead.
Executes the split block for real: a substring pin cannot see TDZ order.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.web_assets import app_js


def test_saved_split_applies_without_throwing(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node not available")
    js = app_js()
    start = js.index('const _SPLIT_KEY = "biblioplex.split";')
    end = js.index("function wireSplitter(")
    script = tmp_path / "split.js"
    script.write_text(
        """
const grid = () => ({ style: { v: {}, setProperty(k, x) { this.v[k] = x; } } });
const cardsSplitEl = grid(), invSplitEl = grid(), wlSplitEl = grid();
const localStorage = { getItem: () => "40" };
const window = { bibliohelpers: { clampSplit: (p) => Number(p) } };
"""
        + js[start:end]
        + """
if (cardsSplitEl.style.v["--split"] !== "40%") { console.error("split not applied"); process.exit(1); }
"""
    )
    proc = subprocess.run([node, str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
