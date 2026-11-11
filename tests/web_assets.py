"""Shared reader for the web client source.

web/js/*.js are concatenated in filename order into one app.js at container
start (web/10-render-app-js.sh). Tests pin strings across that whole bundle.
"""

from __future__ import annotations

from pathlib import Path

WEB_JS = Path(__file__).resolve().parent.parent / "web" / "js"


def app_js() -> str:
    return "".join(p.read_text() for p in sorted(WEB_JS.glob("*.js")))
