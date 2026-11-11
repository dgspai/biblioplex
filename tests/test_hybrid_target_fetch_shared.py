"""D15.17 — pin the shared target-row fetch + neighbour-serialize helpers.

`/similar` and `/substitutes` previously duplicated ~30 LOC of target-row
SELECT + null-check + row-loop tail. D15.17 factors both into
`_fetch_similar_target` and `_serialize_neighbours`; this file pins the
refactor so a future edit that inlines one branch and forgets the other
surfaces in review.

Pure introspection — no HTTP, no DB.
"""

from __future__ import annotations

import inspect

from biblioplex.routers.cards import (
    _TARGET_ROW_SQL,
    _fetch_similar_target,
    _serialize_neighbours,
    get_similar_cards,
    get_substitutes,
)


def test_target_row_sql_is_module_constant() -> None:
    """The shared SELECT lives at module scope — both handlers reference it."""
    assert "SELECT embedding, oracle_search, cmc, color_identity, type_search" in _TARGET_ROW_SQL
    assert "FROM cards c WHERE c.id = :id" in _TARGET_ROW_SQL
    # oracle_tags subquery pulls the target's tag set in the same round trip.
    assert "SELECT array_agg(tag) FROM oracle_tags" in _TARGET_ROW_SQL


def test_fetch_similar_target_is_async() -> None:
    """Helper must be an async coroutine — both handlers `await` it."""
    assert inspect.iscoroutinefunction(_fetch_similar_target)


def test_get_similar_cards_calls_shared_helpers() -> None:
    """`/similar` handler routes through `_fetch_similar_target` + `_serialize_neighbours`.

    Regression guard for the D15.17 factor. If a future edit re-inlines
    the target-row SELECT or the row-loop tail, both branches diverge in
    silence and this pin fails loudly.
    """
    src = inspect.getsource(get_similar_cards)
    assert "_fetch_similar_target" in src
    assert "_serialize_neighbours" in src


def test_get_substitutes_calls_shared_helpers() -> None:
    """`/substitutes` handler routes through the same two helpers."""
    src = inspect.getsource(get_substitutes)
    assert "_fetch_similar_target" in src
    assert "_serialize_neighbours" in src


def test_serialize_neighbours_signature_takes_rows_and_projection() -> None:
    """Helper signature is `(rows, projection_paths) -> list[dict]` per the plan."""
    sig = inspect.signature(_serialize_neighbours)
    params = list(sig.parameters)
    assert params == ["rows", "projection_paths"]
