"""The version lives only in pyproject.toml; the package reads it back."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import biblioplex

ROOT = Path(__file__).resolve().parent.parent


def test_version_matches_pyproject() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert biblioplex.__version__ == project["version"]


def test_init_does_not_hardcode_a_version() -> None:
    src = (ROOT / "src" / "biblioplex" / "__init__.py").read_text()
    # The "0.0.0+unknown" fallback for never-installed trees is allowed.
    literals = re.findall(r'__version__ = "([^"]+)"', src)
    assert all(v.startswith("0.0.0+") for v in literals), literals
