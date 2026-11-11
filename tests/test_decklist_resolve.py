"""D12.3 — batch name resolver."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

import pytest

from biblioplex.decklist.resolve import FUZZY_THRESHOLD, ResolveResult, resolve_names

_SOL = UUID("11111111-1111-1111-1111-111111111111")
_BOLT = UUID("22222222-2222-2222-2222-222222222222")
_LLANOWAR = UUID("33333333-3333-3333-3333-333333333333")


class _Row:
    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


class _Result:
    def __init__(self, rows: Sequence[Any]) -> None:
        self._rows = list(rows)

    def all(self) -> list[Any]:
        return list(self._rows)

    def one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


class _FakeConn:
    """Plays back canned rows per SQL signature.

    Bug.19: resolver now issues exactly two SQL round-trips — one
    batched exact-match SELECT plus one batched trigram fold over the
    misses (`unnest(:misses)` → LATERAL top-1 per miss). `fuzzy_rows`
    is FLAT — the fold returns one row per hit; misses are omitted.
    Legacy per-call `fuzzy_results` is still accepted for backward
    compat: the leading tuples get flattened, and each `_Row` in a
    per-call bucket is stamped with an `input_name` matching the miss
    it stood for (positional — first miss = first non-empty bucket).
    """

    def __init__(
        self,
        exact_rows: Sequence[Any] = (),
        fuzzy_rows: Sequence[Any] | None = None,
        fuzzy_results: Sequence[Sequence[Any]] | None = None,
    ) -> None:
        self._exact = list(exact_rows)
        self._fuzzy: list[Any] = list(fuzzy_rows or [])
        self._legacy_fuzzy: list[list[Any]] | None = (
            [list(b) for b in fuzzy_results] if fuzzy_results is not None else None
        )
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, stmt: Any, params: dict[str, Any]) -> _Result:
        sql = str(stmt)
        self.calls.append((sql, params))
        if "LOWER(c.name) = LOWER(t.n)" in sql:
            return _Result(self._exact)
        if "similarity(c.name_sort_key" in sql:
            if self._legacy_fuzzy is not None:
                # Flatten legacy per-call buckets into flat rows,
                # stamping input_name from the misses bind.
                misses = list(params.get("misses", []))
                out: list[Any] = []
                for i, name in enumerate(misses):
                    bucket = self._legacy_fuzzy[i] if i < len(self._legacy_fuzzy) else []
                    for row in bucket:
                        row.input_name = name
                        out.append(row)
                return _Result(out)
            return _Result(self._fuzzy)
        raise AssertionError(f"unexpected SQL: {sql!r}")


async def test_empty_input_returns_empty_result() -> None:
    conn = _FakeConn()
    out = await resolve_names(conn, [])
    assert out == ResolveResult()
    assert conn.calls == []  # no DB round trip on empty input


async def test_single_exact_match() -> None:
    conn = _FakeConn(exact_rows=[_Row(id=_SOL, input_name="Sol Ring")])
    out = await resolve_names(conn, ["Sol Ring"])
    assert out.matches == {"Sol Ring": _SOL}
    assert out.unresolved == []
    # Only the batch query fired; no fuzzy fallback when the exact pass hits.
    sqls = [s for s, _ in conn.calls]
    assert any("LOWER(c.name) = LOWER(t.n)" in s for s in sqls)
    assert not any("similarity(c.name_sort_key" in s for s in sqls)


async def test_case_insensitive_exact_match() -> None:
    conn = _FakeConn(exact_rows=[_Row(id=_SOL, input_name="Sol Ring")])
    out = await resolve_names(conn, ["SOL RING"])
    assert out.matches == {"SOL RING": _SOL}


async def test_dedup_by_lowered_form() -> None:
    """`Sol Ring` and `sol ring` collapse to one input; first wins."""
    conn = _FakeConn(exact_rows=[_Row(id=_SOL, input_name="Sol Ring")])
    out = await resolve_names(conn, ["Sol Ring", "sol ring", "SOL RING"])
    assert out.matches == {"Sol Ring": _SOL}


async def test_per_miss_fuzzy_fallback_hit() -> None:
    """Typo'd name misses exact, hits trigram fallback."""
    conn = _FakeConn(
        exact_rows=[],  # no exact match
        fuzzy_results=[[_Row(id=_BOLT)]],
    )
    out = await resolve_names(conn, ["lighning bolt"])
    assert out.matches == {"lighning bolt": _BOLT}
    assert out.unresolved == []
    # Exact pass first, fuzzy fallback second.
    sqls = [s for s, _ in conn.calls]
    assert any("LOWER(c.name) = LOWER(t.n)" in s for s in sqls)
    assert any("similarity(c.name_sort_key" in s for s in sqls)


async def test_fuzzy_threshold_bound_into_query() -> None:
    conn = _FakeConn(exact_rows=[], fuzzy_results=[[]])
    await resolve_names(conn, ["nonesuch"])
    fuzzy_call = next((p for s, p in conn.calls if "similarity" in s), None)
    assert fuzzy_call is not None
    assert fuzzy_call["threshold"] == FUZZY_THRESHOLD


async def test_fuzzy_miss_recorded_in_unresolved() -> None:
    """No exact AND no fuzzy hit → ends up in `unresolved`."""
    conn = _FakeConn(exact_rows=[], fuzzy_results=[[]])
    out = await resolve_names(conn, ["zxqyzz"])
    assert out.matches == {}
    assert out.unresolved == ["zxqyzz"]


async def test_mixed_exact_fuzzy_and_unresolved() -> None:
    """One exact, one fuzzy hit, one total miss — order is input order."""
    conn = _FakeConn(
        exact_rows=[_Row(id=_SOL, input_name="Sol Ring")],
        fuzzy_results=[
            [_Row(id=_BOLT)],   # lighning bolt → bolt
            [],                  # zxqyzz → nothing
        ],
    )
    out = await resolve_names(conn, ["Sol Ring", "lighning bolt", "zxqyzz"])
    assert out.matches == {"Sol Ring": _SOL, "lighning bolt": _BOLT}
    assert out.unresolved == ["zxqyzz"]


async def test_input_order_preserved_in_matches() -> None:
    """`matches` iteration order mirrors input order, not exact-then-fuzzy."""
    conn = _FakeConn(
        exact_rows=[
            _Row(id=_LLANOWAR, input_name="Llanowar Elves"),
            _Row(id=_SOL, input_name="Sol Ring"),
        ],
        fuzzy_results=[[_Row(id=_BOLT)]],
    )
    out = await resolve_names(conn, ["Sol Ring", "lighning bolt", "Llanowar Elves"])
    assert list(out.matches.keys()) == ["Sol Ring", "lighning bolt", "Llanowar Elves"]


@pytest.mark.parametrize(
    "exact_rows,fuzzy_results,inputs,expected_matches,expected_unresolved",
    [
        ((), ((),), ("Nothing",), {}, ["Nothing"]),
        (
            (_Row(id=_SOL, input_name="Sol Ring"),),
            (),
            ("Sol Ring",),
            {"Sol Ring": _SOL},
            [],
        ),
    ],
)
async def test_parametrized_smoke(
    exact_rows: tuple[Any, ...],
    fuzzy_results: tuple[tuple[Any, ...], ...],
    inputs: tuple[str, ...],
    expected_matches: dict[str, UUID],
    expected_unresolved: list[str],
) -> None:
    conn = _FakeConn(
        exact_rows=list(exact_rows),
        fuzzy_results=[list(r) for r in fuzzy_results],
    )
    out = await resolve_names(conn, list(inputs))
    assert out.matches == expected_matches
    assert out.unresolved == expected_unresolved


# ---- D13.4: resolve_and_fetch_deck_rows ----

from biblioplex.decklist.resolve import (  # noqa: E402
    ResolvedDeckRow,
    resolve_and_fetch_deck_rows,
)


class _FoldFakeConn:
    """Fake for the single-query fold. Only handles the fold shape."""

    def __init__(self, fold_rows: list[Any]) -> None:
        self._rows = fold_rows
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, stmt: Any, params: dict[str, Any]) -> _Result:
        sql = str(stmt)
        self.calls.append((sql, params))
        if "unnest(" in sql and ":ords" in sql:
            names = list(params.get("names", []))
            # Return only rows whose input_name appears in :names.
            selected = [r for r in self._rows if r.input_name in names]
            return _Result(selected)
        raise AssertionError(f"unexpected SQL: {sql!r}")


async def test_resolve_and_fetch_empty_input() -> None:
    conn = _FoldFakeConn(fold_rows=[])
    resolved, unresolved = await resolve_and_fetch_deck_rows(conn, [])
    assert resolved == []
    assert unresolved == []
    assert conn.calls == []


async def test_resolve_and_fetch_single_query_fires() -> None:
    conn = _FoldFakeConn(
        fold_rows=[
            _Row(input_name="Sol Ring", id=_SOL, name="Sol Ring", type_line="Artifact",
                 layout="normal", card_faces=None, predicted_labels=["ramp"],
                 cmc=1, mana_cost="{1}", color_identity=[]),
        ]
    )
    resolved, unresolved = await resolve_and_fetch_deck_rows(conn, ["Sol Ring"])
    assert len(conn.calls) == 1, "must issue exactly one query"
    assert len(resolved) == 1
    assert isinstance(resolved[0], ResolvedDeckRow)
    assert resolved[0].input_name == "Sol Ring"
    assert resolved[0].row.name == "Sol Ring"
    assert unresolved == []


async def test_resolve_and_fetch_preserves_input_order() -> None:
    """Result list is in input order (not fold-SQL order)."""
    conn = _FoldFakeConn(
        fold_rows=[
            # Reversed order in the fake — the resolver must reorder.
            _Row(input_name="Bolt", id=_BOLT, name="Lightning Bolt",
                 type_line="Instant", layout="normal", card_faces=None,
                 predicted_labels=["targeted_disrupt"], cmc=1, mana_cost="{R}",
                 color_identity=["R"]),
            _Row(input_name="Sol Ring", id=_SOL, name="Sol Ring",
                 type_line="Artifact", layout="normal", card_faces=None,
                 predicted_labels=["ramp"], cmc=1, mana_cost="{1}",
                 color_identity=[]),
        ]
    )
    resolved, _ = await resolve_and_fetch_deck_rows(conn, ["Sol Ring", "Bolt"])
    assert [r.input_name for r in resolved] == ["Sol Ring", "Bolt"]


async def test_resolve_and_fetch_unresolved_when_row_absent() -> None:
    conn = _FoldFakeConn(fold_rows=[])
    resolved, unresolved = await resolve_and_fetch_deck_rows(conn, ["Zxqyzz"])
    assert resolved == []
    assert unresolved == ["Zxqyzz"]


async def test_fold_sql_fuzzy_only_over_misses() -> None:
    """Bug.4: `fuzzy_hits` LATERAL scans only the `misses` CTE.

    Prior shape ran the trigram scan against every input row before
    filtering by `WHERE i.n NOT IN exact_hits`. On a decklist where
    99% exact-match, the LATERAL still fired 100 pg_trgm scans.
    The `misses` CTE gates the LATERAL upstream so only true exact-
    misses trigger a fuzzy scan.
    """
    from biblioplex.decklist.resolve import _RESOLVE_AND_FETCH_SQL

    sql = _RESOLVE_AND_FETCH_SQL
    assert "misses AS" in sql, "expected a `misses` CTE to gate the fuzzy LATERAL"
    # LATERAL must FROM the misses CTE, not from the raw input.
    fuzzy_pos = sql.index("fuzzy_hits AS")
    lateral_pos = fuzzy_pos + sql[fuzzy_pos:].index("CROSS JOIN LATERAL")
    fuzzy_block = sql[fuzzy_pos:lateral_pos]
    assert "FROM misses" in fuzzy_block


async def test_fold_sql_lowers_fuzzy_input_for_trigram() -> None:
    """Bug.5: fuzzy_hits LATERAL passes `LOWER(i.n)` to `similarity()`.

    pg_trgm doesn't case-fold; `name_sort_key` is stored lowercased at
    ingest (schema.sql:74). Passing raw `i.n` scored mixed-case
    decklist entries lower than case-folded twins.
    """
    from biblioplex.decklist.resolve import _RESOLVE_AND_FETCH_SQL

    assert "similarity(c.name_sort_key, LOWER(m.n))" in _RESOLVE_AND_FETCH_SQL, (
        "fuzzy_hits must lower the input name before trigram similarity"
    )


async def test_fold_sql_surfaces_fuzzy_similarity_score() -> None:
    """D15.14: `resolve_and_fetch_deck_rows` returns the fuzzy trigram score.

    The fold's `fuzzy_hits` LATERAL already computes `similarity(...)`
    into `f.s` but currently discards it. Threading it through the CTE
    chain + final SELECT lets consumers (the web UI's D15.4 hover
    preview) surface "matched via fuzzy: 0.62" so users see when the
    deck lookup guessed. Exact hits report `NULL::float`.
    """
    from biblioplex.decklist.resolve import _RESOLVE_AND_FETCH_SQL

    sql = _RESOLVE_AND_FETCH_SQL
    # exact_hits contributes NULL::float for the similarity column so the
    # UNION with fuzzy_hits keeps a single type.
    assert "NULL::float AS similarity" in sql
    # fuzzy_hits exposes f.s under the `similarity` label.
    fuzzy_pos = sql.index("fuzzy_hits AS")
    fuzzy_block = sql[fuzzy_pos:sql.index("resolved AS")]
    assert "f.s AS similarity" in fuzzy_block
    # Final SELECT carries the column so the row proxy exposes it.
    final_select = sql.split("resolved AS", 1)[1].split(")\n", 1)[1]
    assert "similarity" in final_select


async def test_resolve_and_fetch_row_carries_fuzzy_similarity() -> None:
    """`ResolvedDeckRow.similarity` mirrors the SA row's `similarity` column."""
    conn = _FoldFakeConn(
        fold_rows=[
            _Row(input_name="lighning bolt", id=_BOLT, name="Lightning Bolt",
                 type_line="Instant", layout="normal", card_faces=None,
                 predicted_labels=["targeted_disrupt"], cmc=1, mana_cost="{R}",
                 color_identity=["R"], similarity=0.62),
        ]
    )
    resolved, _ = await resolve_and_fetch_deck_rows(conn, ["lighning bolt"])
    assert resolved[0].similarity == pytest.approx(0.62)


async def test_resolve_and_fetch_exact_hit_has_null_similarity() -> None:
    """Exact hits report `similarity=None` — the fuzzy scan didn't run."""
    conn = _FoldFakeConn(
        fold_rows=[
            _Row(input_name="Sol Ring", id=_SOL, name="Sol Ring",
                 type_line="Artifact", layout="normal", card_faces=None,
                 predicted_labels=["ramp"], cmc=1, mana_cost="{1}",
                 color_identity=[], similarity=None),
        ]
    )
    resolved, _ = await resolve_and_fetch_deck_rows(conn, ["Sol Ring"])
    assert resolved[0].similarity is None


async def test_bug17_exact_join_lowers_both_sides_on_db() -> None:
    """Bug.17: `resolve_names` exact SQL folds both operands on the DB side.

    Prior shape mixed Postgres `LOWER(name)` (locale-aware) with a
    Python `.lower()` list — on non-ASCII names the two folds could
    disagree and a valid card silently miss. Fix pushes `LOWER(t.n)` to
    the DB side too so a single collation decides both.
    """
    conn = _FakeConn(exact_rows=[])
    await resolve_names(conn, ["Séance"])
    exact_sql = next(s for s, _ in conn.calls if "LOWER(c.name)" in s)
    assert "LOWER(c.name) = LOWER(t.n)" in exact_sql
    assert "ANY(:lowered)" not in exact_sql


async def test_bug17_fold_sql_lowers_exact_join_on_db() -> None:
    """Bug.17: `_RESOLVE_AND_FETCH_SQL` exact_hits JOIN uses `LOWER(i.n)`."""
    from biblioplex.decklist.resolve import _RESOLVE_AND_FETCH_SQL

    sql = _RESOLVE_AND_FETCH_SQL
    exact_pos = sql.index("exact_hits AS")
    exact_block = sql[exact_pos:sql.index("misses AS")]
    assert "LOWER(c.name) = LOWER(i.n)" in exact_block


async def test_bug19_fuzzy_fallback_folded_into_one_round_trip() -> None:
    """Bug.19: N-miss decklist fires exactly 2 SQL calls (exact + fold).

    Prior shape ran one trigram SELECT per unresolved input; a 5-miss
    decklist paid 6 round-trips. The fold uses `unnest(:misses)` +
    LATERAL top-1 per miss inside a single query.
    """
    conn = _FakeConn(exact_rows=[], fuzzy_rows=[])
    await resolve_names(
        conn,
        ["miss1", "miss2", "miss3", "miss4", "miss5"],
    )
    assert len(conn.calls) == 2, conn.calls
    fuzzy_call = next(p for s, p in conn.calls if "similarity" in s)
    assert fuzzy_call["misses"] == [
        "miss1", "miss2", "miss3", "miss4", "miss5",
    ]


async def test_resolve_names_lowers_fuzzy_input() -> None:
    """Bug.5: fuzzy fold lowers the input name inside `similarity()`.

    Post-Bug.19 the per-miss loop is folded into ONE `unnest(:misses)`
    + LATERAL query; the same lowering still applies via `LOWER(m.n)`.
    """
    conn = _FakeConn(exact_rows=[], fuzzy_rows=[])
    await resolve_names(conn, ["Nonesuch"])
    fuzzy_sql = next(s for s, _ in conn.calls if "similarity" in s)
    assert "LOWER(m.n)" in fuzzy_sql


async def test_resolve_and_fetch_dedup_by_lowered_form() -> None:
    """`Sol Ring` and `SOL RING` → one input in the outbound SQL."""
    conn = _FoldFakeConn(
        fold_rows=[
            _Row(input_name="Sol Ring", id=_SOL, name="Sol Ring",
                 type_line="Artifact", layout="normal", card_faces=None,
                 predicted_labels=["ramp"], cmc=1, mana_cost="{1}",
                 color_identity=[]),
        ]
    )
    resolved, unresolved = await resolve_and_fetch_deck_rows(
        conn, ["Sol Ring", "SOL RING", "sol ring"]
    )
    assert len(resolved) == 1
    assert resolved[0].input_name == "Sol Ring"
    # Only ONE input string was submitted to the SQL (dedup).
    submitted_names = conn.calls[0][1]["names"]
    assert submitted_names == ["Sol Ring"]
