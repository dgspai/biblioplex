"""Settings About line shows the running version from /health."""

from __future__ import annotations

from pathlib import Path

from tests.web_assets import app_js

ROOT = Path(__file__).resolve().parent.parent


def test_about_line_has_version_slot() -> None:
    html = (ROOT / "web" / "index.html").read_text()
    about = html[html.index('<footer class="about">'):]
    about = about[: about.index("</footer>")]
    assert 'id="appVersion"' in about


def test_app_fills_version_from_health() -> None:
    js = app_js()
    assert "h.version" in js
    assert "appVersion" in js
