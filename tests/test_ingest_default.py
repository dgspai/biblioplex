"""Tests for `default_ingest` (D1.8, tech_spec.md §4.2).

`default_ingest` chains the M1 components end-to-end:

1. D1.1 `discover_default_cards_uri` resolves the current bulk download URI.
2. `httpx.AsyncClient.stream` opens the JSON byte stream.
3. D1.2 `iter_cards` (via `ijson.items_async`) yields kept cards.
4. D1.3 `split_reversible` explodes reversible parents into one row per face.
5. D1.4 `flatten_card` populates the `_search` columns + `colors_eff`.
6. Batched `INSERT INTO cards_staging (<STAGING_COLUMNS>) VALUES (...)` writes
   via SQLAlchemy `conn.execute(stmt, list_of_dicts)`.

These tests stub httpx + the engine. The real DB round-trip is M8 smoke.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from biblioplex.ingest.bulk import BULK_DATA_BASE, BULK_DATA_URL
from biblioplex.ingest.collapse import STAGING_COLUMNS
from biblioplex.ingest.refresh import default_ingest

# --- Engine + connection fakes -------------------------------------------


class _FakeResult:
    def __init__(self, value: Any = None) -> None:
        self._value = value

    def scalar_one(self) -> Any:
        return self._value


class _FakeConn:
    """Async-context-manager DB conn that records executemany payloads."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []

    async def __aenter__(self) -> _FakeConn:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        self.executed.append((str(stmt), params))
        return _FakeResult(None)


class _FakeEngine:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def begin(self) -> _FakeConn:
        return self._conn

    def connect(self) -> _FakeConn:
        return self._conn


# --- httpx stubs ----------------------------------------------------------


class _StreamResponse:
    """Stand-in for an `httpx.Response` returned from `client.stream(...)`.

    The async context manager protocol mirrors httpx — `aiter_bytes()`
    yields the raw body in chunks. Bug.34: Scryfall now serves
    `*.jsonl.gz` as `Content-Type: application/gzip` with NO
    `Content-Encoding`, so httpx does NOT decompress — `iter_cards_async`
    sniffs the gzip magic and handles it. These fixtures feed plaintext
    JSONL, which the same sniff passes through untouched.
    """

    def __init__(self, chunks: list[bytes], status_code: int = 200) -> None:
        self._chunks = chunks
        self.status_code = status_code

    async def __aenter__(self) -> _StreamResponse:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"status {self.status_code}",
                request=httpx.Request("GET", "http://x"),
                response=httpx.Response(self.status_code),
            )

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        for c in self._chunks:
            yield c


class _FakeAsyncClient:
    """Replaces `httpx.AsyncClient` for the duration of a test.

    Records GETs, returns the canned discovery payload, and replays the
    canned stream body when `.stream(...)` is called.
    """

    def __init__(
        self,
        *,
        download_uri: str,
        body_chunks: list[bytes],
    ) -> None:
        self._download_uri = download_uri
        self._body_chunks = body_chunks
        self.get_urls: list[str] = []
        self.stream_urls: list[str] = []

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get(self, url: str, **_: Any) -> httpx.Response:
        self.get_urls.append(url)
        return httpx.Response(
            200,
            json={
                "object": "bulk_data",
                "type": "default_cards",
                "jsonl_download_uri": self._download_uri,
                "content_type": "application/json",
                "content_encoding": "gzip",
            },
            request=httpx.Request("GET", url),
        )

    def stream(self, method: str, url: str, **_: Any) -> _StreamResponse:
        assert method == "GET"
        self.stream_urls.append(url)
        return _StreamResponse(self._body_chunks)


# --- Tests ----------------------------------------------------------------


async def test_default_ingest_chains_discovery_stream_and_insert() -> None:
    """Happy path: discovery → stream → flatten → INSERT.

    Verifies:
    * Discovery hits the Scryfall bulk-data metadata endpoint.
    * The discovered URI is what gets streamed.
    * Each kept card lands in an INSERT payload whose dict has every
      `STAGING_COLUMNS` key (and only those keys).
    * `default_ingest` returns the total staged-row count.
    """
    download_uri = "https://data.scryfall.io/default-cards/test.json"
    body = (
        b'{"id":"a","oracle_id":"oa","name":"Lightning Bolt",'
        b'"layout":"normal","released_at":"2025-01-01"}\n'
        b'{"id":"b","oracle_id":"ob","name":"Counterspell",'
        b'"layout":"normal","released_at":"2024-06-01"}\n'
    )
    fake_client = _FakeAsyncClient(download_uri=download_uri, body_chunks=[body])
    conn = _FakeConn()
    engine = _FakeEngine(conn)

    with patch("biblioplex.ingest.refresh.httpx.AsyncClient", lambda **_: fake_client):
        staged = await default_ingest(engine)

    # Discovery → metadata endpoint hit, exactly once.
    assert fake_client.get_urls == [BULK_DATA_URL]
    # Stream → discovered URI.
    assert fake_client.stream_urls == [download_uri]
    # Two cards staged → return value reflects it.
    assert staged == 2

    # Find the INSERT call(s) — at least one.
    inserts = [
        (sql, params) for sql, params in conn.executed if "INSERT INTO cards_staging" in sql
    ]
    assert inserts, f"no staging INSERT seen; executed={conn.executed!r}"

    # Flatten the payloads across all inserts.
    all_rows: list[dict[str, Any]] = []
    for _, params in inserts:
        assert isinstance(params, list), "executemany requires a list of dicts"
        all_rows.extend(params)

    assert len(all_rows) == 2
    for row in all_rows:
        assert set(row.keys()) == set(STAGING_COLUMNS), (
            f"row keys mismatch: got {sorted(row.keys())!r}, "
            f"expected {sorted(STAGING_COLUMNS)!r}"
        )

    # Spot-check D1.4 flatten output: `name_search` populated.
    names = {r["name_search"] for r in all_rows}
    assert "Lightning Bolt" in names
    assert "Counterspell" in names


async def test_default_ingest_honors_bulk_type() -> None:
    """`bulk_type` routes discovery to the requested Scryfall bulk endpoint.

    Guards the `BULK_SOURCE_TYPE` setting: passing `oracle_cards` must hit
    `/bulk-data/oracle-cards`, not the hardcoded default-cards endpoint.
    """
    download_uri = "https://data.scryfall.io/oracle-cards/test.json"
    body = b'{"id":"a","oracle_id":"oa","name":"Bolt","layout":"normal","released_at":"2025-01-01"}\n'
    fake_client = _FakeAsyncClient(download_uri=download_uri, body_chunks=[body])
    engine = _FakeEngine(_FakeConn())

    with patch("biblioplex.ingest.refresh.httpx.AsyncClient", lambda **_: fake_client):
        await default_ingest(engine, bulk_type="oracle_cards")

    assert fake_client.get_urls == [f"{BULK_DATA_BASE}/oracle-cards"]
    assert fake_client.stream_urls == [download_uri]


async def test_default_ingest_handles_reversible_split() -> None:
    """Reversible parents emit one staged row per face per §3.4 / D1.3."""
    download_uri = "https://data.scryfall.io/default-cards/test.json"
    body = (
        b'{"id":"p","oracle_id":"unused","name":"Heads // Tails",'
        b'"layout":"reversible_card","released_at":"2025-01-01",'
        b'"card_faces":['
        b'{"oracle_id":"face-a","name":"Heads","type_line":"Sorcery","oracle_text":"A","mana_cost":"{R}"},'
        b'{"oracle_id":"face-b","name":"Tails","type_line":"Sorcery","oracle_text":"B","mana_cost":"{G}"}'
        b']}\n'
    )
    fake_client = _FakeAsyncClient(download_uri=download_uri, body_chunks=[body])
    conn = _FakeConn()
    engine = _FakeEngine(conn)

    with patch("biblioplex.ingest.refresh.httpx.AsyncClient", lambda **_: fake_client):
        staged = await default_ingest(engine)

    assert staged == 2, "reversible parent must split into two staged rows"

    inserts = [
        params for sql, params in conn.executed if "INSERT INTO cards_staging" in sql
    ]
    rows = [r for batch in inserts for r in batch]
    oracle_ids = {r["oracle_id"] for r in rows}
    assert oracle_ids == {"face-a", "face-b"}


async def test_default_ingest_drops_extras_via_stream_filter() -> None:
    """`iter_cards` already drops tokens/emblems/etc — verify they don't reach staging."""
    download_uri = "https://data.scryfall.io/default-cards/test.json"
    body = (
        b'{"id":"k","oracle_id":"ok","name":"Keep","layout":"normal","released_at":"2025-01-01"}\n'
        b'{"id":"t","oracle_id":"ot","name":"Drop me","layout":"token"}\n'
        b'{"id":"e","oracle_id":"oe","name":"Drop me too","layout":"emblem"}\n'
    )
    fake_client = _FakeAsyncClient(download_uri=download_uri, body_chunks=[body])
    conn = _FakeConn()
    engine = _FakeEngine(conn)

    with patch("biblioplex.ingest.refresh.httpx.AsyncClient", lambda **_: fake_client):
        staged = await default_ingest(engine)

    assert staged == 1
    inserts = [
        params for sql, params in conn.executed if "INSERT INTO cards_staging" in sql
    ]
    rows = [r for batch in inserts for r in batch]
    assert {r["oracle_id"] for r in rows} == {"ok"}


async def test_default_ingest_batches_inserts() -> None:
    """Rows are flushed in batches — verify batching when row count exceeds INGEST_BATCH."""
    download_uri = "https://data.scryfall.io/default-cards/test.json"
    # 300 cards: should produce >1 batch with default batch size 256.
    cards = [
        f'{{"id":"id{i}","oracle_id":"o{i}","name":"C{i}","layout":"normal","released_at":"2025-01-01"}}'
        for i in range(300)
    ]
    body = b"\n".join(c.encode() for c in cards)
    fake_client = _FakeAsyncClient(download_uri=download_uri, body_chunks=[body])
    conn = _FakeConn()
    engine = _FakeEngine(conn)

    with patch("biblioplex.ingest.refresh.httpx.AsyncClient", lambda **_: fake_client):
        staged = await default_ingest(engine)

    assert staged == 300
    inserts = [
        params for sql, params in conn.executed if "INSERT INTO cards_staging" in sql
    ]
    # At least 2 batches because 300 > batch size 256.
    assert len(inserts) >= 2, (
        f"expected at least 2 batches for 300 rows; got {len(inserts)}"
    )
    total = sum(len(b) for b in inserts)
    assert total == 300


@pytest.fixture(autouse=True)
def _isolated() -> None:
    """No-op fixture — keeps pytest-asyncio in scope for these tests."""
    return None
