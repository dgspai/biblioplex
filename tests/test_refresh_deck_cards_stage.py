"""D19.24 — `deck_cards` refresh stage keeps the derived index current.

`rebuild_all_deck_cards` shipped in D19.19 but nothing called it, so decks
predating the index stayed empty forever and resolution drift after a card
refresh was never repaired.
"""

from __future__ import annotations

from typing import Any

import pytest

from biblioplex.ingest.refresh import RefreshRunner, RefreshStatus


class _FakeResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one(self) -> Any:
        return self._value


class _FakeConn:
    def __init__(self) -> None:
        self.executed: list[str] = []

    async def __aenter__(self) -> _FakeConn:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def begin(self) -> _FakeConn:
        return self

    async def execution_options(self, **_kw: Any) -> _FakeConn:
        return self

    async def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        sql = str(stmt)
        self.executed.append(sql)
        if "pg_try_advisory_lock" in sql:
            return _FakeResult(True)
        if "pg_advisory_unlock" in sql:
            return _FakeResult(True)
        return _FakeResult(None)

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _FakeEngine:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def begin(self) -> _FakeConn:
        return self._conn

    def connect(self) -> _FakeConn:
        return self._conn


async def _ingest(_engine: Any) -> int:
    return 3


def _runner(status: RefreshStatus, **kw: Any) -> RefreshRunner:
    return RefreshRunner(_FakeEngine(_FakeConn()), status, ingest=_ingest, **kw)


# ---- registration ----


def test_deck_cards_is_a_known_stage() -> None:
    assert "deck_cards" in RefreshRunner.ALL_STAGES


def test_deck_cards_runs_after_ingest_not_before() -> None:
    """Resolution reads `cards`, so the stage belongs in the post-ingest
    fan-out beside classify/combos — not alongside the independent `tags`."""
    order = list(RefreshRunner.ALL_STAGES)
    assert order.index("deck_cards") > order.index("ingest")


# ---- behaviour ----


@pytest.mark.asyncio
async def test_stage_runs_and_records_its_row_count() -> None:
    calls: list[str] = []

    async def _deck_cards(_engine: Any) -> int:
        calls.append("ran")
        return 7

    status = RefreshStatus()
    status.mark_started("job-1")
    await _runner(status, deck_cards=_deck_cards).run_locked()
    status.mark_finished()

    assert calls == ["ran"]
    stage = next(s for s in status.snapshot().stages if s.name == "deck_cards")
    assert stage.rows == 7
    assert stage.error is None


@pytest.mark.asyncio
async def test_stage_failure_does_not_abort_the_refresh() -> None:
    """Best-effort, like tags/classify/combos.

    A deck-index failure must never lose a completed card ingest, and must
    not strand the advisory lock.
    """
    async def _boom(_engine: Any) -> int:
        raise RuntimeError("index rebuild exploded")

    status = RefreshStatus()
    status.mark_started("job-1")
    staged = await _runner(status, deck_cards=_boom).run_locked()
    status.mark_finished()

    assert staged == 3, "a best-effort stage must not swallow the ingest result"
    stage = next(s for s in status.snapshot().stages if s.name == "deck_cards")
    assert stage.error is not None
    assert "index rebuild exploded" in stage.error


@pytest.mark.asyncio
async def test_stage_can_be_selected_alone() -> None:
    """`POST /admin/refresh {"stages":["deck_cards"]}` is the repair path —
    it must not drag a 30-minute ingest along with it."""
    ran: list[str] = []

    async def _deck_cards(_engine: Any) -> int:
        ran.append("deck_cards")
        return 2

    async def _ingest_spy(_engine: Any) -> int:
        ran.append("ingest")
        return 3

    status = RefreshStatus()
    status.mark_started("job-1")
    runner = RefreshRunner(
        _FakeEngine(_FakeConn()), status,
        ingest=_ingest_spy, deck_cards=_deck_cards,
    )
    await runner.run_locked(frozenset({"deck_cards"}))
    status.mark_finished()

    assert ran == ["deck_cards"]


@pytest.mark.asyncio
async def test_stage_is_skipped_when_not_selected() -> None:
    ran: list[str] = []

    async def _deck_cards(_engine: Any) -> int:
        ran.append("deck_cards")
        return 0

    status = RefreshStatus()
    status.mark_started("job-1")
    await _runner(status, deck_cards=_deck_cards).run_locked(frozenset({"ingest"}))
    status.mark_finished()

    assert ran == []
    assert all(s.name != "deck_cards" for s in status.snapshot().stages)


# ---- default body ----


@pytest.mark.asyncio
async def test_default_body_rebuilds_every_deck() -> None:
    """Rebuilding ALL decks is the point: it backfills decks predating the
    index AND repairs resolution drift for the rest."""
    from biblioplex.ingest import refresh as refresh_mod

    seen: list[Any] = []

    async def _fake_rebuild(conn: Any) -> int:
        seen.append(conn)
        return 4

    engine = _FakeEngine(_FakeConn())
    original = refresh_mod.rebuild_all_deck_cards
    refresh_mod.rebuild_all_deck_cards = _fake_rebuild  # type: ignore[assignment]
    try:
        rows = await refresh_mod.default_deck_cards(engine)
    finally:
        refresh_mod.rebuild_all_deck_cards = original  # type: ignore[assignment]

    assert rows == 4
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_default_body_uses_a_write_transaction() -> None:
    """D18.3: `connect()` opens a txn that is ROLLED BACK on close, so a
    rebuild through it would silently write nothing."""
    from biblioplex.ingest import refresh as refresh_mod

    used: list[str] = []

    class _Engine:
        def begin(self) -> _FakeConn:
            used.append("begin")
            return _FakeConn()

        def connect(self) -> _FakeConn:
            used.append("connect")
            return _FakeConn()

    async def _fake_rebuild(_conn: Any) -> int:
        return 0

    original = refresh_mod.rebuild_all_deck_cards
    refresh_mod.rebuild_all_deck_cards = _fake_rebuild  # type: ignore[assignment]
    try:
        await refresh_mod.default_deck_cards(_Engine())
    finally:
        refresh_mod.rebuild_all_deck_cards = original  # type: ignore[assignment]

    assert used == ["begin"]
