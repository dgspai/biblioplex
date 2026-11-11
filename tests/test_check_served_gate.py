"""D19.12 — the served-vs-shipped gate is wired and cannot false-pass.

`make check-web` proves the TEMPLATE parses. It never makes an HTTP
request, so it cannot prove the container serves it. These pins guard the
wiring that closes that gap, so the check cannot be quietly dropped from
the gate the way the gap itself went unnoticed for four deliverables.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_served.sh"


@pytest.fixture(scope="module")
def makefile() -> str:
    return (ROOT / "Makefile").read_text()


def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.is_file()
    assert os.access(SCRIPT, os.X_OK), "make check invokes it; it must be runnable"


def test_gate_runs_the_served_check(makefile: str) -> None:
    """`make check` must include it, or the gap reopens silently."""
    check_line = next(
        line for line in makefile.splitlines() if line.startswith("check:")
    )
    assert "check-served-soft" in check_line


def test_a_standalone_target_exists_for_hard_failure(makefile: str) -> None:
    assert "\ncheck-served:" in makefile


def test_script_is_syntactically_valid() -> None:
    proc = subprocess.run(
        ["bash", "-n", str(SCRIPT)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr


def test_unreachable_stack_fails_by_default() -> None:
    """Silence would be a false pass — the whole failure this closes."""
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        env={**os.environ, "CHECK_SERVED_URL": "http://127.0.0.1:9"},
        cwd=ROOT,
    )
    assert proc.returncode != 0
    assert "nothing serving" in (proc.stdout + proc.stderr)


def test_unreachable_stack_skips_loudly_in_soft_mode() -> None:
    """`make check` must stay usable with no stack — but say so out loud."""
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--skip-if-absent"],
        capture_output=True,
        text=True,
        env={**os.environ, "CHECK_SERVED_URL": "http://127.0.0.1:9"},
        cwd=ROOT,
    )
    assert proc.returncode == 0
    assert "SKIPPED" in proc.stdout, "a silent skip is indistinguishable from a pass"


def test_every_served_asset_is_covered() -> None:
    """All four files the image ships must be compared.

    web/Dockerfile COPYs index.html, style.css, helpers.js and
    web/js/; missing one recreates the gap for that file alone.
    """
    body = SCRIPT.read_text()
    for asset in ("app.js", "index.html", "style.css", "helpers.js"):
        assert f'"{asset}"' in body, asset


def test_default_q_is_read_from_the_container_not_guessed() -> None:
    """app.js is envsubst-rendered at container start.

    Rendering locally with a different DEFAULT_Q reports a mismatch that
    is not real, which would train everyone to ignore the check.
    """
    body = SCRIPT.read_text()
    assert "printenv DEFAULT_Q" in body
