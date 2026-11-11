"""D19.19 — derived `deck_cards` index.

The decklist text stays the source of truth; these rows are regenerated
from it. The load-bearing property is that `deck_cards` IS a partition
even though `parse_sections` deliberately is not.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from biblioplex.decks import service as deck_service
from biblioplex.decks.service import deck_card_rows

EXPORTS = Path(__file__).resolve().parent.parent / "data" / "example_deck_exports"


def _total(rows: list[Any]) -> int:
    return sum(r.qty for r in rows)


# ---- the partition property ----


def test_rows_are_a_partition_unlike_parse_sections() -> None:
    """`main` contains the commander (D19.10); these rows must not.

    Writing `parse_sections` buckets straight into rows double-counts the
    commander, so `SUM(qty)` reports a 101-card Commander deck.
    """
    rows = deck_card_rows(
        "Commander\n1 Hope of Ghirapur\nDeck\n1 Sol Ring\nSideboard\n1 Grapeshot\n"
    )
    assert _total(rows) == 3
    by_section: dict[str, list[Any]] = {}
    for r in rows:
        by_section.setdefault(r.section, []).append(r)
    assert [r.card_name for r in by_section["commander"]] == ["Hope of Ghirapur"]
    assert [r.card_name for r in by_section["main"]] == ["Sol Ring"]
    assert [r.card_name for r in by_section["sideboard"]] == ["Grapeshot"]


def test_commander_kept_as_a_copy_elsewhere_is_not_lost() -> None:
    """Subtraction is by QUANTITY, not by name.

    Deck is 1 commander plus 2 more copies in the body = 3 cards. D19.10
    injects the commander's copy into `main`, so `main` holds 3; removing
    exactly the commander's 1 leaves the body's 2 intact. Removing by name
    would wipe all three and under-count the deck.
    """
    rows = deck_card_rows("Commander\n1 Sol Ring\nDeck\n2 Sol Ring\n")
    assert _total(rows) == 3
    main = [r for r in rows if r.section == "main"]
    assert [(r.card_name, r.qty) for r in main] == [("Sol Ring", 2)]


def test_companion_is_its_own_section_and_not_duplicated_into_main() -> None:
    rows = deck_card_rows(
        "Commander\n1 Hope of Ghirapur\n"
        "Companion\n1 Lurrus of the Dream-Den\n"
        "Deck\n1 Sol Ring\n"
    )
    assert _total(rows) == 3


def test_sum_matches_decklist_counts_on_a_real_export() -> None:
    """The invariant that makes coverage arithmetic trustworthy."""
    text = (EXPORTS / "archidekt" / "colorless_arsenal.txt").read_text()
    total, _distinct, _names = deck_service.decklist_counts(text)
    assert _total(deck_card_rows(text)) == total == 100


def test_headless_decklist_is_all_mainboard() -> None:
    rows = deck_card_rows("1 Sol Ring\n4 Lightning Bolt\n")
    assert {r.section for r in rows} == {"main"}
    assert _total(rows) == 5


def test_duplicate_lines_fold_into_one_row() -> None:
    rows = deck_card_rows("1 Sol Ring\n2 Sol Ring\n")
    assert [(r.card_name, r.qty) for r in rows] == [("Sol Ring", 3)]


# ---- persistence ----


def _result(rows: list[dict[str, Any]] | None = None) -> MagicMock:
    rows = rows or []
    mappings = MagicMock()
    mappings.all.return_value = rows
    mappings.one.return_value = rows[0] if rows else None
    mappings.one_or_none.return_value = rows[0] if rows else None
    res = MagicMock()
    res.mappings.return_value = mappings
    return res


def _conn(*results: MagicMock) -> AsyncMock:
    conn = AsyncMock()
    it = iter(results)

    def _next(*_a: Any, **_k: Any) -> Any:
        try:
            return next(it)
        except StopIteration:
            return _result()

    conn.execute.side_effect = _next
    return conn


def _sqls(conn: AsyncMock) -> list[str]:
    return [str(c.args[0]) for c in conn.execute.await_args_list]


@pytest.mark.asyncio
async def test_rebuild_deletes_before_inserting() -> None:
    """Rebuild is delete-then-insert; without the delete a re-sync stacks
    duplicate rows forever, exactly as D18.2's merge path had to handle."""
    conn = _conn()
    await deck_service.rebuild_deck_cards(conn, "11111111-1111-1111-1111-111111111111",
                                          "1 Sol Ring\n")
    sqls = _sqls(conn)
    assert "DELETE FROM deck_cards" in sqls[0]
    assert "INSERT INTO deck_cards" in sqls[1]


@pytest.mark.asyncio
async def test_rebuild_resolves_names_to_oracle_ids() -> None:
    conn = _conn()
    await deck_service.rebuild_deck_cards(conn, "11111111-1111-1111-1111-111111111111",
                                          "1 Sol Ring\n")
    assert any("oracle_id" in s for s in _sqls(conn))


@pytest.mark.asyncio
async def test_rebuild_of_an_empty_decklist_still_clears(deck_id: str = "1" * 8 + "-1111-1111-1111-111111111111") -> None:
    """An emptied deck must not keep its old rows."""
    conn = _conn()
    await deck_service.rebuild_deck_cards(conn, deck_id, "")
    sqls = _sqls(conn)
    assert "DELETE FROM deck_cards" in sqls[0]
    assert not any("INSERT INTO deck_cards" in s for s in sqls)


@pytest.mark.asyncio
async def test_rebuild_uses_cast_not_double_colon() -> None:
    """Bug.27 discipline."""
    conn = _conn()
    await deck_service.rebuild_deck_cards(conn, "11111111-1111-1111-1111-111111111111",
                                          "1 Sol Ring\n")
    for sql in _sqls(conn):
        assert "::uuid" not in sql


@pytest.mark.asyncio
async def test_rebuild_all_walks_every_deck() -> None:
    """Backfill and post-refresh repair share one implementation."""
    conn = _conn(
        _result([
            {"id": "11111111-1111-1111-1111-111111111111", "decklist": "1 Sol Ring\n"},
            {"id": "22222222-2222-2222-2222-222222222222", "decklist": "1 Mana Crypt\n"},
        ])
    )
    assert await deck_service.rebuild_all_deck_cards(conn) == 2
    sqls = _sqls(conn)
    assert sum("DELETE FROM deck_cards" in s for s in sqls) == 2
    assert sum("INSERT INTO deck_cards" in s for s in sqls) == 2


@pytest.mark.asyncio
async def test_rebuild_all_tolerates_a_deck_with_no_versions() -> None:
    """LEFT JOIN yields NULL rather than skipping the deck."""
    conn = _conn(
        _result([{"id": "11111111-1111-1111-1111-111111111111", "decklist": None}])
    )
    assert await deck_service.rebuild_all_deck_cards(conn) == 1
