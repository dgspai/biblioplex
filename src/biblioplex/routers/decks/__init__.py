"""``/decks/*`` routers — D16.37 split of the former 2231-line `decks.py`.

One module per endpoint group (`{analyze, bracket, combos, upgrade,
suggest, manabase, synergy, price, diff, compare, goldfish, legality,
export, mulligan, sideboard, near_combos}.py`), each carrying its own
`APIRouter(prefix="/decks", tags=["decks"])`. Genuinely shared helpers
(the D19.9 section-routing trio, the commander-CI resolver, the
analyze/suggest tally, `DECKLIST_MAX_LEN`, `_ERROR_RESPONSE`) live in
`_common.py` — see that module's docstring.

`router` below assembles the sixteen sub-routers with one
`include_router` each; this is a pure move, so the resulting route set,
URLs, and behavior are byte-identical to the pre-split module.

Re-exports kept for external importers (unchanged import paths):
`router` (`main.py`), `DECKLIST_MAX_LEN` (`routers/deck_library.py`),
`SIDEBOARD_SLUGS` (`tests/test_decks_sideboard_suggest.py`), and the
two Bug.27-pinned SQL constants `_COMBOS_SUBSET_SQL` /
`_COMBOS_COUNT_SQL` (`tests/test_bug27_repo_scan.py`).
"""

from __future__ import annotations

from fastapi import APIRouter

from ._common import DECKLIST_MAX_LEN
from .analyze import router as _analyze_router
from .bracket import _COMBOS_COUNT_SQL
from .bracket import router as _bracket_router
from .combos import _COMBOS_SUBSET_SQL
from .combos import router as _combos_router
from .compare import router as _compare_router
from .diff import router as _diff_router
from .export import router as _export_router
from .goldfish import router as _goldfish_router
from .legality import router as _legality_router
from .manabase import router as _manabase_router
from .mulligan import router as _mulligan_router
from .near_combos import router as _near_combos_router
from .price import router as _price_router
from .sideboard import SIDEBOARD_SLUGS
from .sideboard import router as _sideboard_router
from .suggest import router as _suggest_router
from .synergy import router as _synergy_router
from .upgrade import router as _upgrade_router

router = APIRouter()
router.include_router(_analyze_router)
router.include_router(_compare_router)
router.include_router(_diff_router)
router.include_router(_export_router)
router.include_router(_price_router)
router.include_router(_suggest_router)
router.include_router(_sideboard_router)
router.include_router(_combos_router)
router.include_router(_near_combos_router)
router.include_router(_synergy_router)
router.include_router(_mulligan_router)
router.include_router(_goldfish_router)
router.include_router(_bracket_router)
router.include_router(_upgrade_router)
router.include_router(_manabase_router)
router.include_router(_legality_router)

__all__ = [
    "router",
    "DECKLIST_MAX_LEN",
    "SIDEBOARD_SLUGS",
    "_COMBOS_SUBSET_SQL",
    "_COMBOS_COUNT_SQL",
]
