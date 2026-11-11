"""Bulk-data discovery — tech_spec.md §4.1.

Calls Scryfall's ``/bulk-data/<type>`` metadata endpoint and returns the
current download URI for the bulk file. The URI changes whenever Scryfall
publishes a new dump (roughly daily), so we must discover it per ingest run
rather than hard-code it.

Bug.34 (2026-08-17): Scryfall migrated the bulk files from a single JSON
array (``download_uri``, ``*.json``, gzipped transparently by the CDN via
``Content-Encoding``) to JSON Lines (``jsonl_download_uri``, ``*.jsonl.gz``,
served as ``Content-Type: application/gzip`` with NO ``Content-Encoding``).
The old key is simply absent from the response now, so refresh died with
"bulk-data response missing non-empty 'download_uri' field". We prefer the
new key and still accept the old one, so a rollback on their side — or a
recorded fixture from before the change — keeps working.
"""

from __future__ import annotations

import httpx

BULK_DATA_URL = "https://api.scryfall.com/bulk-data/default-cards"
BULK_DATA_BASE = "https://api.scryfall.com/bulk-data"


class BulkDataError(RuntimeError):
    """Raised when Scryfall's bulk-data metadata response is unusable.

    Covers HTTP failures (non-2xx), missing ``download_uri`` field, and
    empty-string URIs. The refresh runner converts these into a logged
    failure for the current run; the worker stays alive for next attempt.
    """


async def discover_bulk_uri(client: httpx.AsyncClient, bulk_type: str) -> str:
    """Fetch the current download URI for any Scryfall bulk-data type.

    Args:
        client: An :class:`httpx.AsyncClient`. Caller owns its lifecycle.
        bulk_type: Bulk type slug (``default_cards``, ``oracle_tags``, ...).
            Scryfall's endpoint uses dash-form (`/bulk-data/oracle-tags`);
            we convert underscores → dashes for the caller's convenience.

    Returns:
        The ``jsonl_download_uri`` from the bulk-data metadata response,
        falling back to the legacy ``download_uri`` when present.

    Raises:
        BulkDataError: On any non-200 response, network error wrapped by
            httpx, malformed JSON, missing download-URI key, or empty
            URI value.
    """
    slug = bulk_type.replace("_", "-")
    url = f"{BULK_DATA_BASE}/{slug}"
    try:
        response = await client.get(url)
    except httpx.HTTPError as exc:  # pragma: no cover — exercised via integration
        raise BulkDataError(f"bulk-data fetch failed: {exc}") from exc

    if response.status_code != 200:
        raise BulkDataError(
            f"bulk-data fetch returned HTTP {response.status_code}: {response.text[:200]}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise BulkDataError(f"bulk-data response was not JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise BulkDataError("bulk-data response was not a JSON object")

    # New key first (Bug.34); legacy key kept so a Scryfall rollback or an
    # older recorded response still resolves.
    uri = payload.get("jsonl_download_uri") or payload.get("download_uri")
    if not isinstance(uri, str) or not uri:
        raise BulkDataError(
            "bulk-data response missing non-empty 'jsonl_download_uri' "
            "(or legacy 'download_uri') field"
        )
    if not uri.startswith(("http://", "https://")):
        raise BulkDataError(f"bulk-data download URI missing scheme: {uri!r}")

    return uri


async def discover_default_cards_uri(client: httpx.AsyncClient) -> str:
    """Back-compat shim — equivalent to :func:`discover_bulk_uri` ``("default_cards")``."""
    return await discover_bulk_uri(client, "default_cards")
