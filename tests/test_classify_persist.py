"""``default_classify`` — template-classifier-plan.md Step 5 persistence.

Drives the refresh runner's classify step over a stubbed engine that
yields one page of fake rows, captures the UPDATE payload, and asserts
the verdict matches ``classify_by_rules``. No DB; the contract under
test is the SQL shape + payload mapping, not Postgres semantics.
"""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from biblioplex.classify.rules import LABELS, classify_by_rules
from biblioplex.ingest.refresh import default_classify


class _StubResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _Row:
    """Row-shaped fake for ``.id / .oracle_text / .card_faces / .label_reasons``."""

    def __init__(
        self,
        id_: str,
        oracle_text: str,
        card_faces: Any = None,
        label_reasons: Any = None,
    ) -> None:
        self.id = id_
        self.oracle_text = oracle_text
        self.card_faces = card_faces
        self.label_reasons = label_reasons


class _StubConn:
    """Single-shot fake: first execute() returns rows, subsequent ones return [] (EOF)."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows
        self._select_calls = 0
        self.update_payloads: list[list[dict[str, Any]]] = []

    def begin(self) -> _StubCtx:
        return _StubCtx()

    async def execute(self, stmt: Any, params: Any = None) -> _StubResult:
        sql = str(stmt).strip().lower()
        if sql.startswith("select"):
            self._select_calls += 1
            return _StubResult(self._rows if self._select_calls == 1 else [])
        # UPDATE — capture the payload.
        self.update_payloads.append(params)
        return _StubResult([])


class _StubCtx:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_: object) -> None:
        return None


class _StubConnCtx:
    def __init__(self, conn: _StubConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _StubConn:
        return self._conn

    async def __aexit__(self, *_: object) -> None:
        return None


class _StubEngine:
    def __init__(self, conn: _StubConn) -> None:
        self._conn = conn

    def connect(self) -> _StubConnCtx:
        return _StubConnCtx(self._conn)


async def test_default_classify_writes_labels_and_scores() -> None:
    """For each row, payload carries label list + jsonb score map."""
    rows = [
        _Row("id-bolt", "Lightning Bolt deals 3 damage to any target."),
        _Row("id-sol", "{T}: Add {C}{C}."),
        _Row("id-wrath", "Destroy all creatures."),
    ]
    conn = _StubConn(rows)
    n = await default_classify(_StubEngine(conn))

    assert n == 3
    assert len(conn.update_payloads) == 1
    payload = conn.update_payloads[0]
    assert len(payload) == 3
    by_id = {p["id"]: p for p in payload}
    # Spot-check: each entry's labels match the classifier's >=0.5 cut,
    # and scores serialize via psycopg's Jsonb wrapper.
    for row in rows:
        expected_scores = classify_by_rules({
            "oracle_text": row.oracle_text,
            "card_faces": row.card_faces,
        })
        expected_labels = [lab for lab in LABELS if expected_scores[lab] >= 0.5]
        entry = by_id[row.id]
        assert entry["labels"] == expected_labels
        assert isinstance(entry["scores"], Jsonb)
        # D15.3: label_reasons persisted alongside scores.
        assert isinstance(entry["reasons"], Jsonb)


async def test_default_classify_preserves_manual_override() -> None:
    """A row carrying a `_override` in label_reasons keeps the pinned labels
    (not the rules verdict) and re-stamps `_override`, so a re-classify never
    wipes a human correction."""
    # Oracle text the rules classifier would score as ramp — but the override
    # pins it to card_advantage, and that must win.
    row = _Row(
        "id-override",
        "{T}: Add {G}{G}.",
        label_reasons={"_override": ["card_advantage"]},
    )
    conn = _StubConn([row])
    await default_classify(_StubEngine(conn))

    entry = conn.update_payloads[0][0]
    assert entry["labels"] == ["card_advantage"]
    assert entry["reasons"].obj["_override"] == ["card_advantage"]


async def test_default_classify_returns_zero_when_empty() -> None:
    """Empty cards table — no payload, no error, returns 0."""
    conn = _StubConn(rows=[])
    n = await default_classify(_StubEngine(conn))
    assert n == 0
    assert conn.update_payloads == []


async def test_default_classify_sql_uses_keyset_pagination() -> None:
    """D15.10: SELECT uses `WHERE id > :last_id` instead of OFFSET.

    OFFSET pagination on a 30k+ row table pays O(offset) rows-skipped
    per page. Keyset (`WHERE id > :last_id ORDER BY id LIMIT :limit`)
    is O(1) per page — index-driven range scan.
    """
    from biblioplex.ingest.refresh import _CLASSIFY_SELECT_SQL

    assert "WHERE id > :last_id" in _CLASSIFY_SELECT_SQL
    assert "OFFSET" not in _CLASSIFY_SELECT_SQL


class _KeysetConn:
    """Multi-page fake that threads :last_id per select call.

    Returns the next page whose ids are strictly greater than the
    supplied :last_id. Assert the runner threads the maximum id from
    each page as the next :last_id.
    """

    def __init__(self, pages: list[list[_Row]]) -> None:
        # Flatten to one canonical ordered store; each SELECT gets rows
        # with id > :last_id, capped at page size (from the query).
        self._all: list[_Row] = [r for p in pages for r in p]
        self._all.sort(key=lambda r: r.id)
        self.select_calls: list[str] = []
        self.update_payloads: list[list[dict[str, Any]]] = []
        self._page_size = max(len(p) for p in pages) if pages else 1

    def begin(self) -> _StubCtx:
        return _StubCtx()

    async def execute(self, stmt: Any, params: Any = None) -> _StubResult:
        sql = str(stmt).strip().lower()
        if sql.startswith("select"):
            last_id = params["last_id"] if params else ""
            self.select_calls.append(last_id)
            hits = [r for r in self._all if r.id > last_id][: self._page_size]
            return _StubResult(hits)
        self.update_payloads.append(params)
        return _StubResult([])


class _KeysetEngine:
    def __init__(self, conn: _KeysetConn) -> None:
        self._conn = conn

    def connect(self) -> Any:
        conn = self._conn

        class _Ctx:
            async def __aenter__(self) -> Any:
                return conn

            async def __aexit__(self, *_: object) -> None:
                return None

        return _Ctx()


async def test_default_classify_threads_last_id_between_pages() -> None:
    """D15.10: each page's max `id` becomes the next call's `:last_id`."""
    pages = [
        [_Row("id-a", "{T}: Add {C}."), _Row("id-b", "Destroy all creatures.")],
        [_Row("id-c", "Lightning Bolt deals 3 damage to any target.")],
    ]
    conn = _KeysetConn(pages)
    n = await default_classify(_KeysetEngine(conn))

    assert n == 3
    # First call bootstraps with empty / zero UUID; subsequent calls
    # thread the max id from the prior page.
    assert conn.select_calls[0] in ("", "00000000-0000-0000-0000-000000000000")
    assert conn.select_calls[1] == "id-b"
    assert conn.select_calls[2] == "id-c"
