"""Pytest bootstrap — make this worktree's ``src/`` the import root.

The venv has biblioplex installed editable against the main checkout's
``src/`` directory. When pytest runs from inside a git worktree we want
its tests to see *this* worktree's code, not the main checkout's, so
agents (and `git bisect`) can iterate without reinstalling.

Prepending ``<worktree>/src`` to ``sys.path`` ensures Python's import
machinery resolves ``biblioplex`` to the worktree files first.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Settings requires DATABASE_URL, and locally it comes from .env. CI and a
# fresh clone have no .env, so give tests a dummy: they use fake engines and
# never connect. An exported DATABASE_URL still wins; a .env value is shadowed,
# which is fine because no test opens a real connection.
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost:5432/test")

_SRC = Path(__file__).resolve().parent.parent / "src"
_SRC_STR = str(_SRC)
if _SRC_STR not in sys.path:
    sys.path.insert(0, _SRC_STR)
