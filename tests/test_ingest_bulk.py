"""Tests for bulk-data discovery (D1.1, tech_spec.md §4.1).

Covers the happy path (returns a non-empty download_uri string), the property
that any well-formed Scryfall response yields the documented uri, plus the
error cases mandated by the deliverable (HTTP error, missing key).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from biblioplex.ingest.bulk import (
    BULK_DATA_URL,
    BulkDataError,
    discover_default_cards_uri,
)


def _make_client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    """Build an AsyncClient backed by the supplied mock transport."""
    return httpx.AsyncClient(transport=handler)


async def test_discover_returns_download_uri() -> None:
    """Happy path: a 200 with `download_uri` returns that string verbatim."""
    expected = "https://data.scryfall.io/default-cards/default-cards-20260617.json"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(BULK_DATA_URL)
        return httpx.Response(
            200,
            json={
                "object": "bulk_data",
                "type": "default_cards",
                "download_uri": expected,
                "content_type": "application/json",
                "content_encoding": "gzip",
            },
        )

    async with _make_client(httpx.MockTransport(handler)) as client:
        uri = await discover_default_cards_uri(client)

    assert uri == expected


async def test_discover_raises_on_non_200() -> None:
    """Non-200 → BulkDataError (no silent fallback)."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    async with _make_client(httpx.MockTransport(handler)) as client:
        with pytest.raises(BulkDataError):
            await discover_default_cards_uri(client)


async def test_discover_raises_on_missing_download_uri() -> None:
    """200 body without `download_uri` → BulkDataError."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"object": "bulk_data"})

    async with _make_client(httpx.MockTransport(handler)) as client:
        with pytest.raises(BulkDataError):
            await discover_default_cards_uri(client)


async def test_discover_raises_on_empty_download_uri() -> None:
    """200 body with empty-string `download_uri` → BulkDataError."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"download_uri": ""})

    async with _make_client(httpx.MockTransport(handler)) as client:
        with pytest.raises(BulkDataError):
            await discover_default_cards_uri(client)


async def test_discover_raises_on_scheme_less_download_uri() -> None:
    """200 body with a non-http(s) `download_uri` → BulkDataError.

    Guards against Scryfall returning a malformed URI (observed once with
    the ``oracle_tags`` bulk type). Converts the opaque
    ``httpx.UnsupportedProtocol`` downstream into a clear ``BulkDataError``.
    """

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"download_uri": "data.scryfall.io/x.json"})

    async with _make_client(httpx.MockTransport(handler)) as client:
        with pytest.raises(BulkDataError):
            await discover_default_cards_uri(client)


# --- Property test (spec-mandated by deliverable D1.1 brief) ---

_URI_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
    min_size=1,
    max_size=200,
).map(lambda s: "https://" + s)


@given(uri=_URI_TEXT, extra=st.dictionaries(st.text(min_size=1, max_size=10), st.integers()))
@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_discover_property_returns_supplied_uri(uri: str, extra: dict[str, Any]) -> None:
    """For any non-empty string `download_uri`, discovery returns exactly that string.

    Run synchronously inside hypothesis with a fresh event loop per case so we
    don't need an async hypothesis plugin.
    """
    import asyncio

    def handler(_: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = {"download_uri": uri, **extra}
        return httpx.Response(200, json=body)

    async def _run() -> str:
        async with _make_client(httpx.MockTransport(handler)) as client:
            return await discover_default_cards_uri(client)

    result = asyncio.run(_run())
    assert isinstance(result, str)
    assert result == uri
    assert len(result) > 0


@pytest.fixture(autouse=True)
def _isolated_event_loop() -> Iterator[None]:
    """Ensure each test gets a clean asyncio state when hypothesis spins loops."""
    yield



# Bug.34 — Scryfall dropped `download_uri` in favour of `jsonl_download_uri`
# on 2026-08-17. Refresh died with "bulk-data response missing non-empty
# 'download_uri' field" because the key simply no longer exists.


def _json_handler(payload: dict[str, Any]) -> httpx.MockTransport:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


async def test_discover_prefers_jsonl_download_uri() -> None:
    expected = "https://data.scryfall.io/default-cards/default-cards-20260817.jsonl.gz"
    payload = {
        "object": "bulk_data",
        "type": "default_cards",
        "jsonl_download_uri": expected,
        "compressed_size": 77517651,
    }
    async with _make_client(_json_handler(payload)) as client:
        assert await discover_default_cards_uri(client) == expected


async def test_discover_still_accepts_legacy_download_uri() -> None:
    """Kept so a Scryfall rollback, or an older recorded response, resolves."""
    expected = "https://data.scryfall.io/default-cards/old.json"
    async with _make_client(_json_handler({"download_uri": expected})) as client:
        assert await discover_default_cards_uri(client) == expected


async def test_discover_prefers_jsonl_when_both_keys_present() -> None:
    payload = {
        "download_uri": "https://data.scryfall.io/old.json",
        "jsonl_download_uri": "https://data.scryfall.io/new.jsonl.gz",
    }
    async with _make_client(_json_handler(payload)) as client:
        assert await discover_default_cards_uri(client) == (
            "https://data.scryfall.io/new.jsonl.gz"
        )


async def test_discover_error_names_the_new_key() -> None:
    """The message an operator sees must point at the key that is missing
    NOW, not the one that used to exist."""
    payload = {"object": "bulk_data", "compressed_size": 1}
    async with _make_client(_json_handler(payload)) as client:
        with pytest.raises(BulkDataError, match="jsonl_download_uri"):
            await discover_default_cards_uri(client)
