"""Portuguese printed-name resolver (decklist/resolve_pt.py)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from biblioplex.decklist.resolve_pt import resolve_pt_names

_FOG = UUID("11111111-1111-1111-1111-111111111111")
_SOL = UUID("22222222-2222-2222-2222-222222222222")
_BOLT = UUID("33333333-3333-3333-3333-333333333333")


class _Row:
    def __init__(self, input_name: str, oracle_id: UUID) -> None:
        self.input_name = input_name
        self.oracle_id = oracle_id


class _Result:
    def __init__(self, rows: Sequence[Any]) -> None:
        self._rows = list(rows)

    def all(self) -> list[Any]:
        return list(self._rows)


class _FakeConn:
    """Plays back canned rows per SQL pass signature.

    Buckets keyed by pass: ``pt_exact``, ``pt_fuzzy``, ``en_exact``.
    Each maps an already-resolved input (case exact) to an oracle id.
    The fake honours the ``:names`` / ``:misses`` bind so a pass only
    returns rows for inputs actually handed to it, exactly like Postgres.
    """

    def __init__(
        self,
        pt_exact: dict[str, UUID] | None = None,
        pt_fuzzy: dict[str, UUID] | None = None,
        en_exact: dict[str, UUID] | None = None,
        en_fuzzy: dict[str, UUID] | None = None,
    ) -> None:
        self.pt_exact = pt_exact or {}
        self.pt_fuzzy = pt_fuzzy or {}
        self.en_exact = en_exact or {}
        self.en_fuzzy = en_fuzzy or {}
        self.calls: list[str] = []

    async def execute(self, stmt: Any, params: dict[str, Any]) -> _Result:
        sql = str(stmt)
        if "similarity(LOWER(p.printed_name)" in sql:
            bucket, inputs = self.pt_fuzzy, params["misses"]
            self.calls.append("pt_fuzzy")
        elif "FROM pt_names p" in sql:
            bucket, inputs = self.pt_exact, params["names"]
            self.calls.append("pt_exact")
        elif "similarity(c.name_sort_key" in sql:
            bucket, inputs = self.en_fuzzy, params["misses"]
            self.calls.append("en_fuzzy")
        elif "FROM cards c" in sql:
            bucket, inputs = self.en_exact, params["names"]
            self.calls.append("en_exact")
        else:  # pragma: no cover - guards against a new unrouted pass
            raise AssertionError(f"unexpected SQL: {sql}")
        rows = [_Row(n, bucket[n]) for n in inputs if n in bucket]
        return _Result(rows)


async def test_pt_exact_hit() -> None:
    conn = _FakeConn(pt_exact={"Banco de Nevoeiro": _FOG})
    result = await resolve_pt_names(conn, ["Banco de Nevoeiro"])
    assert result.matches == {"Banco de Nevoeiro": _FOG}
    assert result.unresolved == []
    assert conn.calls == ["pt_exact"]  # no fuzzy/en pass when exact covers all


async def test_pt_fuzzy_fallback() -> None:
    conn = _FakeConn(pt_fuzzy={"Banco de Nevoiro": _FOG})  # typo'd accent
    result = await resolve_pt_names(conn, ["Banco de Nevoiro"])
    assert result.matches == {"Banco de Nevoiro": _FOG}
    assert conn.calls == ["pt_exact", "pt_fuzzy"]


async def test_english_fallback_for_untranslated_card() -> None:
    # LigaMagic shows the English name for a never-in-PT card.
    conn = _FakeConn(en_exact={"Aggressive Biomancy": _BOLT})
    result = await resolve_pt_names(conn, ["Aggressive Biomancy"])
    assert result.matches == {"Aggressive Biomancy": _BOLT}
    assert conn.calls == ["pt_exact", "pt_fuzzy", "en_exact"]


async def test_english_fuzzy_fallback_for_unofficial_pt_rendering() -> None:
    # LigaMagic shows an unofficial PT rendering ("Jyoti, Ancestral Moag")
    # of a never-in-PT card whose real English name is "Jyoti, Moag
    # Ancient" — matches neither pt_names nor cards.name exactly, only EN
    # fuzzy.
    conn = _FakeConn(en_fuzzy={"Jyoti, Ancestral Moag": _BOLT})
    result = await resolve_pt_names(conn, ["Jyoti, Ancestral Moag"])
    assert result.matches == {"Jyoti, Ancestral Moag": _BOLT}
    assert conn.calls == ["pt_exact", "pt_fuzzy", "en_exact", "en_fuzzy"]


async def test_mixed_batch_preserves_input_order_and_reports_misses() -> None:
    conn = _FakeConn(
        pt_exact={"Anel Solar": _SOL},
        en_exact={"Aggressive Biomancy": _BOLT},
    )
    result = await resolve_pt_names(
        conn, ["Anel Solar", "Aggressive Biomancy", "Carta Fantasma"]
    )
    assert list(result.matches) == ["Anel Solar", "Aggressive Biomancy"]
    assert result.matches == {"Anel Solar": _SOL, "Aggressive Biomancy": _BOLT}
    assert result.unresolved == ["Carta Fantasma"]


async def test_case_folded_dedup() -> None:
    conn = _FakeConn(pt_exact={"anel solar": _SOL})
    # Duplicate by case fold — one input survives, exact pass sees it once.
    result = await resolve_pt_names(conn, ["anel solar", "Anel Solar"])
    assert result.matches == {"anel solar": _SOL}


async def test_empty_input_short_circuits() -> None:
    conn = _FakeConn()
    result = await resolve_pt_names(conn, [])
    assert result.matches == {}
    assert result.unresolved == []
    assert conn.calls == []  # no DB round-trips at all
