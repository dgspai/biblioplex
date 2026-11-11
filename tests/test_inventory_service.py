"""Unit tests for the inventory persistence service (D18.2 / D18.3)."""

from __future__ import annotations

from typing import Any

import pytest

from biblioplex.inventory.deckbox import ParsedDeckboxItem
from biblioplex.inventory.service import (
    resolve_and_sync_inventory,
    update_card_quantity,
)

_SOL = "11111111-1111-1111-1111-111111111111"


class _Mappings:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)

    def one(self) -> Any:
        return self._rows[0]

    def one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def mappings(self) -> _Mappings:
        return _Mappings(self._rows)


class _FakeConn:
    """Substring-routed fake connection recording every (sql, params)."""

    def __init__(self, name_rows: list[dict[str, Any]] | None = None) -> None:
        self.name_rows = name_rows if name_rows is not None else []
        self.squash_rows: list[dict[str, Any]] = []
        self.calls: list[tuple[str, Any]] = []

    async def execute(self, statement: Any, parameters: Any = None) -> _Result:
        sql = " ".join(str(statement).split())
        self.calls.append((sql, parameters))
        if "regexp_replace" in sql:
            # D18.15 whitespace-insensitive fallback — returns its own row
            # shape (`squashed`), so tests opt in explicitly.
            return _Result(self.squash_rows)
        if sql.startswith("SELECT oracle_id, name"):
            return _Result(self.name_rows)
        if sql.startswith("SELECT name FROM cards"):
            return _Result(self.name_rows)
        return _Result([])

    def sql_matching(self, needle: str) -> list[tuple[str, Any]]:
        return [c for c in self.calls if needle in c[0]]


def _sol_row() -> dict[str, Any]:
    return {"oracle_id": _SOL, "name": "Sol Ring", "lower_name": "sol ring"}


@pytest.mark.asyncio
async def test_replace_mode_clears_whole_source_then_inserts() -> None:
    conn = _FakeConn([_sol_row()])
    out = await resolve_and_sync_inventory(
        conn,
        [ParsedDeckboxItem(card_name="Sol Ring", quantity=2)],
        mode="replace",
    )
    deletes = conn.sql_matching("DELETE FROM inventory")
    assert len(deletes) == 1
    assert "WHERE source = :source" in deletes[0][0]
    assert "oracle_id = ANY" not in deletes[0][0]
    assert out["total_cards"] == 2
    assert out["distinct_cards"] == 1
    assert out["unmatched"] == []


@pytest.mark.asyncio
async def test_merge_mode_deletes_only_incoming_oracle_ids() -> None:
    """Regression: a bare insert would stack duplicate rows every merge."""
    conn = _FakeConn([_sol_row()])
    await resolve_and_sync_inventory(
        conn,
        [ParsedDeckboxItem(card_name="Sol Ring", quantity=1)],
        mode="merge",
    )
    deletes = conn.sql_matching("DELETE FROM inventory")
    assert len(deletes) == 1
    assert "oracle_id = ANY(CAST(:oracle_ids AS uuid[]))" in deletes[0][0]
    assert deletes[0][1]["oracle_ids"] == [_SOL]


@pytest.mark.asyncio
async def test_insert_is_a_single_executemany_call() -> None:
    conn = _FakeConn([_sol_row()])
    await resolve_and_sync_inventory(
        conn,
        [
            ParsedDeckboxItem(card_name="Sol Ring", quantity=1, set_name="Alpha"),
            ParsedDeckboxItem(card_name="Sol Ring", quantity=3, set_name="Revised"),
        ],
        mode="replace",
    )
    inserts = conn.sql_matching("INSERT INTO inventory (oracle_id")
    assert len(inserts) == 1
    assert isinstance(inserts[0][1], list)
    assert len(inserts[0][1]) == 2


@pytest.mark.asyncio
async def test_unmatched_names_are_reported_not_inserted() -> None:
    conn = _FakeConn([_sol_row()])
    out = await resolve_and_sync_inventory(
        conn,
        [
            ParsedDeckboxItem(card_name="Sol Ring", quantity=1),
            ParsedDeckboxItem(card_name="Nonexistent Card", quantity=4),
        ],
        mode="replace",
    )
    assert out["unmatched"] == ["Nonexistent Card"]
    assert out["total_cards"] == 1


@pytest.mark.asyncio
async def test_update_card_quantity_upserts_manual_row() -> None:
    conn = _FakeConn([{"name": "Sol Ring"}])
    out = await update_card_quantity(conn, oracle_id=_SOL, quantity=3)
    assert out is not None
    assert out["quantity"] == 3
    assert out["source"] == "manual"
    assert len(conn.sql_matching("DELETE FROM inventory")) == 1
    inserts = conn.sql_matching("INSERT INTO inventory (oracle_id")
    assert len(inserts) == 1
    assert inserts[0][1]["quantity"] == 3


@pytest.mark.asyncio
async def test_update_card_quantity_zero_deletes_without_insert() -> None:
    conn = _FakeConn([{"name": "Sol Ring"}])
    out = await update_card_quantity(conn, oracle_id=_SOL, quantity=0)
    assert out is not None
    assert out["quantity"] == 0
    assert len(conn.sql_matching("DELETE FROM inventory")) == 1
    assert conn.sql_matching("INSERT INTO inventory (oracle_id") == []


@pytest.mark.asyncio
async def test_update_card_quantity_unknown_oracle_returns_none() -> None:
    conn = _FakeConn([])
    assert await update_card_quantity(conn, oracle_id=_SOL, quantity=1) is None
    assert conn.sql_matching("DELETE FROM inventory") == []


@pytest.mark.asyncio
async def test_whitespace_insensitive_fallback_resolves_punctuation_drift() -> None:
    """Scryfall writes `With Great Power . . .`, Deckbox writes
    `With Great Power...`. Found on a real 1235-row wishlist: 1 miss."""
    conn = _FakeConn([])  # exact + split passes miss
    conn.squash_rows = [
        {
            "oracle_id": _SOL,
            "name": "With Great Power . . .",
            "squashed": "withgreatpower...",
        }
    ]
    out = await resolve_and_sync_inventory(
        conn, [ParsedDeckboxItem(card_name="With Great Power...", quantity=1)]
    )
    assert out["unmatched"] == []
    assert out["total_cards"] == 1
    # the canonical Scryfall spelling is what gets stored
    inserts = conn.sql_matching("INSERT INTO inventory (oracle_id")
    assert inserts[0][1][0]["card_name"] == "With Great Power . . ."


@pytest.mark.asyncio
async def test_squash_fallback_skipped_when_everything_resolved() -> None:
    """The fallback is an unindexable scan — it must not run for a fully
    resolved sync, which is the normal case."""
    conn = _FakeConn([_sol_row()])
    await resolve_and_sync_inventory(
        conn, [ParsedDeckboxItem(card_name="Sol Ring", quantity=1)]
    )
    assert not conn.sql_matching("regexp_replace")


def test_squash_ws_collapses_punctuation_spacing() -> None:
    from biblioplex.inventory.service import _squash_ws

    assert _squash_ws("a . . .") == _squash_ws("a...")
    assert _squash_ws("Fire // Ice") == "Fire//Ice"
