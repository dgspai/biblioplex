"""Oracle-tag ingest — Scryfall Tagger bulk file → ``oracle_tags`` table.

Pipeline:

1. Resolve the current ``oracle_tags`` bulk download URI via
   :func:`discover_bulk_uri`.
2. Fetch the gzipped JSON dump (httpx auto-decompresses).
3. ``json.loads`` the whole payload — ~17 MB gzipped, ~50 MB raw, ~4.5k
   tags. ijson buys nothing here because we need the full graph in memory
   to walk ``child_ids`` recursively.
4. For every tag, accumulate the set of ``oracle_id``s from itself plus
   every descendant (parent tags carry empty ``taggings``; their cards
   live on the leaves). Memoized so each subtree is walked once.
5. Emit ``(oracle_id, slug)`` rows for the tag's own slug AND every entry
   in ``aliases``; Scryfall's ``otag:`` accepts alias slugs interchangeably.
6. DELETE + batched INSERT … ON CONFLICT DO NOTHING in one txn.

DELETE (not TRUNCATE) so readers keep hitting the pre-refresh snapshot
under MVCC for the whole rebuild window. TRUNCATE takes ACCESS EXCLUSIVE
which blocks every /similar, /tags, DSL `otag:` reader for the entire
txn — with 543k rows the batched INSERT loop exceeded the 5 s read
statement_timeout and every touching endpoint 500'd until commit.

Tag bulk shape (verified against Scryfall 2026-06):
```
{
  "object": "tag", "id": <uuid>, "type": "oracle",
  "slug": "tutor-creature-giant", "label": "...",
  "parent_ids": [<uuid>...], "child_ids": [<uuid>...],
  "aliases": ["tutor-giant"],
  "taggings": [{"oracle_id": <uuid>, "weight": "median"}, ...]
}
```

Weight is intentionally dropped (matches Scryfall's own ``otag:``
semantics — boolean tagged/not-tagged). See plan in commit history.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

import httpx
from sqlalchemy import text

from biblioplex import SCRYFALL_BULK_TIMEOUT, SCRYFALL_HEADERS
from biblioplex.ingest.bulk import discover_bulk_uri
from biblioplex.ingest.stream import iter_jsonl_bytes

_log = logging.getLogger(__name__)

# Same batch size as ingest staging — keeps memory + executemany payloads
# in the same ballpark across the pipeline.
TAG_INSERT_BATCH: int = 1000

_INSERT_SQL = (
    "INSERT INTO oracle_tags (oracle_id, tag) "
    "VALUES (:oracle_id, :tag) "
    "ON CONFLICT (oracle_id, tag) DO NOTHING"
)
_CLEAR_SQL = "DELETE FROM oracle_tags;"


class _Engine(Protocol):
    """Minimal subset of SQLAlchemy ``AsyncEngine`` we touch."""

    def begin(self) -> Any: ...


def _collect_oracle_ids(
    tag_id: str,
    tags_by_id: dict[str, dict[str, Any]],
    memo: dict[str, set[str]],
    stack: set[str],
) -> set[str]:
    """DFS-collect ``oracle_id``s for ``tag_id`` and every descendant.

    Memoized via ``memo``. ``stack`` guards against cycles (shouldn't
    happen per Scryfall, but trusting community data is unwise).
    """
    if tag_id in memo:
        return memo[tag_id]
    if tag_id in stack:
        return set()
    tag = tags_by_id.get(tag_id)
    if tag is None:
        return set()
    stack.add(tag_id)
    out: set[str] = set()
    for tg in tag.get("taggings") or ():
        oid = tg.get("oracle_id") if isinstance(tg, dict) else None
        if isinstance(oid, str):
            out.add(oid)
    for child_id in tag.get("child_ids") or ():
        if isinstance(child_id, str):
            out |= _collect_oracle_ids(child_id, tags_by_id, memo, stack)
    stack.discard(tag_id)
    memo[tag_id] = out
    return out


def build_rows(tags: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Flatten the tag graph into ``[{"oracle_id", "tag"}, ...]`` rows.

    Pulled out of :func:`ingest_oracle_tags` so the expansion logic is
    unit-testable without a DB or network round-trip.
    """
    tags_by_id: dict[str, dict[str, Any]] = {}
    for t in tags:
        tid = t.get("id") if isinstance(t, dict) else None
        if isinstance(tid, str):
            tags_by_id[tid] = t

    memo: dict[str, set[str]] = {}
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for tag in tags:
        tid = tag.get("id")
        if not isinstance(tid, str):
            continue
        slug = tag.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        # Slug + every alias resolve to the same set of taggings — emit
        # rows for each so DSL queries against either form hit.
        slugs: list[str] = [slug.lower()]
        for alias in tag.get("aliases") or ():
            if isinstance(alias, str) and alias:
                slugs.append(alias.lower())
        oids = _collect_oracle_ids(tid, tags_by_id, memo, set())
        for oid in oids:
            for s in slugs:
                key = (oid, s)
                if key in seen:
                    continue
                seen.add(key)
                rows.append({"oracle_id": oid, "tag": s})
    return rows


async def fetch_tag_bulk(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Discover + download + parse the oracle-tags bulk file."""
    uri = await discover_bulk_uri(client, "oracle_tags")
    resp = await client.get(uri, timeout=120.0)
    resp.raise_for_status()
    # Bug.34: the bulk file is JSON Lines now, not a JSON array, and it
    # arrives still gzipped (Content-Type: application/gzip, no
    # Content-Encoding, so httpx does not decompress it for us).
    # `iter_jsonl_bytes` sniffs the gzip magic, so a pre-decompressed body
    # (older recording, or a future Content-Encoding) also works.
    data = list(iter_jsonl_bytes(resp.content))
    if not data:
        raise ValueError("oracle-tags bulk was empty")
    return data


async def ingest_oracle_tags(engine: _Engine) -> int:
    """Run the full oracle-tag refresh against ``engine``.

    Returns the number of (oracle_id, tag) rows written. Wraps the whole
    thing in one transaction so a partial failure leaves the table in
    its pre-run state.
    """
    async with httpx.AsyncClient(timeout=SCRYFALL_BULK_TIMEOUT, headers=SCRYFALL_HEADERS) as client:
        tags = await fetch_tag_bulk(client)
    rows = build_rows(tags)
    if not rows:
        _log.warning("oracle-tag ingest produced 0 rows; skipping truncate")
        return 0

    insert_stmt = text(_INSERT_SQL)
    async with engine.begin() as conn:
        await conn.execute(text(_CLEAR_SQL))
        for i in range(0, len(rows), TAG_INSERT_BATCH):
            await conn.execute(insert_stmt, rows[i : i + TAG_INSERT_BATCH])
    _log.info("oracle-tag ingest wrote %d rows from %d tags", len(rows), len(tags))
    return len(rows)
