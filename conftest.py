"""Top-level conftest: ensure this worktree's `src/` shadows any other.

The dev venv is installed in editable mode from one specific worktree's `src`
directory. When running from a different worktree, the editable pth points at
the wrong tree and new modules (e.g. `biblioplex.dsl`) added here are invisible.
Inserting this worktree's `src/` at the front of `sys.path` makes the local
copy win.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = (Path(__file__).parent / "src").resolve()
_SRC_STR = str(_SRC)
if _SRC_STR not in sys.path:
    sys.path.insert(0, _SRC_STR)
