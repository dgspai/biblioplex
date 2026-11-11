"""Run web/test_helpers.html's checks under node so the gate covers helpers.js.

The page is a browser harness (open it by hand); this executes the same inline
script against a stub `window`/`document`, so no browser or new dependency.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "web"


@pytest.mark.skipif(shutil.which("node") is None, reason="node missing")
def test_helpers_html_checks_pass(tmp_path: Path) -> None:
    html = (WEB / "test_helpers.html").read_text()
    inline = html.rsplit("<script>", 1)[1].split("</script>", 1)[0]
    shim = (
        "global.window = global;\n"
        "global.document = {getElementById: () => ({textContent: ''})};\n"
        f"require({str(WEB / 'helpers.js')!r});\n"
    )
    script = tmp_path / "run.js"
    script.write_text(shim + inline + "\nprocess.exit(fail ? 1 : 0);\n")
    # fail is a top-level `let` inside the inline script, so it stays in scope.
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, "\n".join(
        ln for ln in proc.stdout.splitlines() if ln.startswith(("FAIL", "      "))
    ) or proc.stderr
