"""Commander Spellbook combo ingest — D14.1.

Optional data source. Populates the ``combos`` side table with card
combos (pairs / triples that produce a known named outcome) sourced from
Commander Spellbook or any operator-supplied JSON list matching the same
shape. The refresh runner wires this step after the oracle-tags block as
best-effort: fetch / parse failure leaves the previous combos table
intact, cards refresh stays committed (same contract as
:func:`biblioplex.ingest.tags.ingest_oracle_tags`).

On by default: ``COMBOS_SOURCE_URL`` points at Commander Spellbook's bulk
file (``variants.json.gz``), downloaded once per refresh and streamed with
``ijson``. Set it empty to disable. Each variant is mapped by
:func:`_map_v2_variant` to the repo shape below.

Repo combo shape (per combo, after mapping):

.. code-block:: json

    {
      "id": "spellbook-123",
      "cards": ["Isochron Scepter", "Dramatic Reversal"],
      "color_identity": "U",
      "prereqs": "Non-land permanents that tap for >= 3 mana.",
      "steps": "Imprint Dramatic Reversal, activate Scepter.",
      "results": ["Infinite mana", "Win the game"]
    }

Card names resolve against ``cards.name`` (case-insensitive). Combos with
any unresolved card are dropped silently — Spellbook may reference cards
we haven't ingested yet (bulk publishing lag) or split-card names that
don't line up letter-for-letter with our top-level ``name``.
"""

from __future__ import annotations

import asyncio
import gzip
import logging
import tempfile
from collections.abc import AsyncIterator, Iterable, Iterator
from typing import IO, Any, Protocol

import httpx
import ijson
from sqlalchemy import text

from biblioplex import SCRYFALL_HEADERS

_log = logging.getLogger(__name__)

# Same batch size as the oracle-tag ingest — keeps memory + executemany
# payloads consistent across the ingest pipeline.
COMBOS_INSERT_BATCH: int = 500

# Canonical color order for `color_identity` normalization. Non-WUBRG
# letters are silently dropped so a malformed source can't poison the
# array — same forgiving posture as the tag ingest.
_WUBRG: str = "WUBRG"
_WUBRG_SET: frozenset[str] = frozenset(_WUBRG)

# ``CAST(... AS uuid[])`` — psycopg passes array as ``text[]``, Postgres
# needs coerce to hit column's ``uuid[]`` type. SQLAlchemy ``text()``
# treats ``:name::type`` as escaped colon and skips the bindparam, so
# ``CAST()`` is the safe form. Other text[] columns need no cast.
_INSERT_SQL: str = (
    "INSERT INTO combos (id, oracle_ids, color_identity, prereqs, steps, results) "
    "VALUES (:id, CAST(:oracle_ids AS uuid[]), :color_identity, :prereqs, :steps, :results) "
    "ON CONFLICT (id) DO UPDATE SET "
    "oracle_ids = EXCLUDED.oracle_ids, "
    "color_identity = EXCLUDED.color_identity, "
    "prereqs = EXCLUDED.prereqs, "
    "steps = EXCLUDED.steps, "
    "results = EXCLUDED.results"
)

# Bug.27: `CAST(:names AS text[])` not `:names::text[]`. SQLAlchemy
# `text()` treats `::` as an escaped colon and skips the preceding
# `:name` bindparam — same reason `_INSERT_SQL` above wraps the array
# arg in ``CAST()``.
_RESOLVE_NAMES_SQL: str = (
    "SELECT c.oracle_id, t.n AS input_name FROM cards c "
    "JOIN unnest(CAST(:names AS text[])) AS t(n) "
    "ON LOWER(c.name) = LOWER(t.n)"
)


class _Engine(Protocol):
    """Minimal subset of SQLAlchemy ``AsyncEngine`` we touch."""

    def begin(self) -> Any: ...

    def connect(self) -> Any: ...


def _normalize_color_identity(value: Any) -> list[str]:
    """Coerce Spellbook's ``color_identity`` field to a canonical WUBRG list.

    Accepts a string (``"WUB"``, ``"wubrg"``) or a list of single-letter
    strings (``["W", "U", "B"]``). Non-WUBRG letters are dropped; result
    is uppercase and in canonical WUBRG order. Missing or malformed input
    returns an empty list — an empty CI matches every commander (5c) via
    the subset check downstream, which is the safe fallback.
    """
    if isinstance(value, str):
        letters = {c.upper() for c in value if c.upper() in _WUBRG_SET}
    elif isinstance(value, list):
        letters = set()
        for item in value:
            if isinstance(item, str) and len(item) == 1:
                up = item.upper()
                if up in _WUBRG_SET:
                    letters.add(up)
    else:
        return []
    return [c for c in _WUBRG if c in letters]


def _normalize_results(value: Any) -> list[str]:
    """Coerce ``results`` to a ``list[str]``. Accepts str / list / None."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def build_rows(
    combos: Iterable[dict[str, Any]],
    name_to_oracle: dict[str, str],
) -> list[dict[str, Any]]:
    """Normalize combo dicts into insert-ready rows.

    Args:
        combos: Iterable of raw combo dicts (Spellbook-shaped, see module
            docstring).
        name_to_oracle: Lowercased card name → oracle_id string. Combos
            referencing any name absent from this map are dropped.

    Returns:
        A list of dicts keyed by :data:`_INSERT_SQL` bind names. Combos
        with duplicate ``id`` are deduplicated (first occurrence wins).
    """
    seen_ids: set[str] = set()
    rows: list[dict[str, Any]] = []
    for combo in combos:
        if not isinstance(combo, dict):
            continue
        cid = combo.get("id")
        if not isinstance(cid, str) or not cid:
            continue
        if cid in seen_ids:
            continue
        cards = combo.get("cards")
        if not isinstance(cards, list) or not cards:
            continue

        oracle_ids: list[str] = []
        missing = False
        for name in cards:
            if not isinstance(name, str) or not name:
                missing = True
                break
            oid = name_to_oracle.get(name.lower())
            if oid is None:
                missing = True
                break
            oracle_ids.append(oid)
        if missing:
            continue

        prereqs = combo.get("prereqs")
        if not isinstance(prereqs, str) or not prereqs:
            prereqs = None
        steps = combo.get("steps")
        if not isinstance(steps, str) or not steps:
            steps = None

        seen_ids.add(cid)
        rows.append(
            {
                "id": cid,
                "oracle_ids": oracle_ids,
                "color_identity": _normalize_color_identity(
                    combo.get("color_identity")
                ),
                "prereqs": prereqs,
                "steps": steps,
                "results": _normalize_results(combo.get("results")),
            }
        )
    return rows


def _map_v2_variant(variant: dict[str, Any]) -> dict[str, Any] | None:
    """Map one Spellbook v2 ``variants/`` entry to the repo combo shape.

    v2 shape (abbreviated)::

        {
          "id": "...",
          "uses": [{"card": {"name": "..."}}, ...],
          "identity": "WUB" | ["W","U","B"],
          "notablePrerequisites": "..." | null,
          "easyPrerequisites": "..." | null,
          "description": "...",
          "produces": [{"feature": {"name": "Infinite mana"}}, ...]
        }

    Missing required fields (id, uses) → ``None`` (caller drops).
    Prereqs fall back from ``notablePrerequisites`` to ``easyPrerequisites``.
    Defensive throughout — a partial variant collapses to sensible defaults
    rather than crashing the fetch loop.
    """
    if not isinstance(variant, dict):
        return None
    cid = variant.get("id")
    if not isinstance(cid, str) or not cid:
        return None
    uses = variant.get("uses")
    if not isinstance(uses, list) or not uses:
        return None
    # Bug.20: any malformed `use` drops the WHOLE variant. Prior per-entry
    # `continue` truncated the required-cards list, producing a combo with
    # fewer cards than the source declared — downstream deck-subset match
    # then false-positived on decks missing the dropped card.
    cards: list[str] = []
    for u in uses:
        if not isinstance(u, dict):
            return None
        card = u.get("card")
        if not isinstance(card, dict):
            return None
        name = card.get("name")
        if not isinstance(name, str) or not name:
            return None
        cards.append(name)
    if not cards:
        return None
    produces = variant.get("produces") or []
    results: list[str] = []
    if isinstance(produces, list):
        for p in produces:
            if not isinstance(p, dict):
                continue
            feature = p.get("feature")
            if isinstance(feature, dict):
                name = feature.get("name")
                if isinstance(name, str) and name:
                    results.append(name)
    prereqs = variant.get("notablePrerequisites") or variant.get("easyPrerequisites")
    if not isinstance(prereqs, str) or not prereqs:
        prereqs = None
    steps = variant.get("description")
    if not isinstance(steps, str) or not steps:
        steps = None
    return {
        "id": cid,
        "cards": cards,
        "color_identity": variant.get("identity"),
        "prereqs": prereqs,
        "steps": steps,
        "results": results,
    }


# Variants per yielded batch: bounds both memory (~50 MB peak streaming the
# 729 MB file) and the size of each upsert transaction.
_FETCH_BATCH: int = 5000

_GZIP_MAGIC: bytes = b"\x1f\x8b"


def _iter_variant_batches(
    raw: IO[bytes], url: str
) -> Iterator[tuple[list[dict[str, Any]], int]]:
    """Stream ``variants.item`` out of the bulk file in mapped batches.

    Sync on purpose: ``ijson`` + ``gzip`` are blocking, so the async caller
    steps this generator with :func:`asyncio.to_thread`. Yields
    ``(mapped_batch, raw_count)``; ``raw_count`` includes variants that
    :func:`_map_v2_variant` dropped (Bug.25 log accuracy).
    """
    gzipped = raw.read(2) == _GZIP_MAGIC
    raw.seek(0)
    f: IO[bytes] | gzip.GzipFile = gzip.GzipFile(fileobj=raw) if gzipped else raw
    batch: list[dict[str, Any]] = []
    raw_count = 0
    total = 0
    try:
        for variant in ijson.items(f, "variants.item", use_float=True):
            raw_count += 1
            mapped = _map_v2_variant(variant) if isinstance(variant, dict) else None
            if mapped is not None:
                batch.append(mapped)
            if raw_count == _FETCH_BATCH:
                yield batch, raw_count
                total += raw_count
                batch, raw_count = [], 0
    except ijson.JSONError as exc:
        raise ValueError(f"combo source at {url!r} is not valid JSON: {exc}") from exc
    if raw_count:
        yield batch, raw_count
        total += raw_count
    if total == 0:
        raise ValueError(
            f"combo source at {url!r} has no `variants`; expected Commander "
            "Spellbook's bulk file (https://json.commanderspellbook.com/variants.json.gz)"
        )


async def _iter_combo_batches(
    client: httpx.AsyncClient, url: str
) -> AsyncIterator[tuple[list[dict[str, Any]], int]]:
    """Download Spellbook's bulk file once, then yield mapped batches.

    Spellbook asks consumers not to page through ``/variants/`` for the
    whole dataset and publishes this file instead (one GET, ~32 MB gzipped).
    Its S3 host sends ``Content-Encoding: gzip`` even for identity requests,
    so the raw (still-compressed) bytes go to a temp file and are sniffed
    for the gzip magic; a plain ``variants.json`` works the same way.
    """
    with tempfile.TemporaryFile() as tmp:
        async with client.stream(
            "GET", url, headers={"Accept-Encoding": "identity"}
        ) as resp:
            resp.raise_for_status()
            async for chunk in resp.aiter_raw():
                tmp.write(chunk)
        tmp.seek(0)
        batches = _iter_variant_batches(tmp, url)
        while (item := await asyncio.to_thread(next, batches, None)) is not None:
            yield item


async def resolve_names_to_oracle_ids(
    engine: _Engine, names: Iterable[str]
) -> dict[str, str]:
    """Batch-resolve card names to ``oracle_id`` strings.

    One round-trip via :data:`_RESOLVE_NAMES_SQL` — `LOWER(c.name) =
    LOWER(t.n)` join against an `unnest(CAST(:names AS text[]))` input
    CTE. Missing names are absent from the returned map (callers treat
    them as unresolved).

    Bug.17: prior shape was `WHERE LOWER(name) = ANY(:lowered)` with
    ``:lowered`` built via Python `.lower()`. Postgres `LOWER()` is
    locale-aware; Python `str.lower()` is Unicode-simple — on non-ASCII
    names (`Séance`, `Æther Vial`) the two can disagree and a valid
    card silently misses. Push the fold to the DB side for both operands
    so a single collation decides both. Returned map is keyed by
    ``t.n.lower()`` on the Python side so callers doing
    ``name_to_oracle.get(name.lower())`` see the same key they built.
    """
    unique_names = sorted({n for n in names if isinstance(n, str) and n})
    if not unique_names:
        return {}
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(_RESOLVE_NAMES_SQL),
                {"names": unique_names},
            )
        ).all()
    return {row.input_name.lower(): str(row.oracle_id) for row in rows}


async def ingest_combos_from_url(engine: _Engine, url: str) -> int:
    """Incremental refresh: upsert per batch, then delete stale IDs.

    Returns the number of combo rows written. When ``url`` is empty /
    falsy, returns 0 without touching the network or the table.

    Each batch's rows land in their own transaction with ``ON CONFLICT DO
    UPDATE`` (columns other than ``id`` are overwritten), so the DB grows
    visibly during the run and existing combos pick up upstream edits.
    After the last batch, combos whose IDs weren't seen this run are
    deleted. A crash mid-run leaves the table with a mix of old + new —
    no data loss; the next successful run reconciles.
    """
    if not url:
        return 0
    _log.info("combo ingest starting: url=%s", url)

    insert_stmt = text(_INSERT_SQL)
    seen_ids: set[str] = set()
    name_to_oracle: dict[str, str] = {}
    tried_names: set[str] = set()  # lowercased; avoids re-querying misses
    total_written = 0
    total_dropped = 0
    total_seen = 0

    async with httpx.AsyncClient(timeout=120.0, headers=SCRYFALL_HEADERS) as client:
        async for page, raw_page_count in _iter_combo_batches(client, url):
            # Bug.25: count RAW source variants (including ones _map_v2_variant
            # dropped as malformed) so the "N source combos" log line matches
            # what the upstream feed actually delivered.
            total_seen += raw_page_count
            if not page:
                continue
            page_names: set[str] = set()
            for combo in page:
                if isinstance(combo, dict):
                    for name in combo.get("cards") or ():
                        if isinstance(name, str) and name:
                            page_names.add(name)
            new_names = {n for n in page_names if n.lower() not in tried_names}
            if new_names:
                resolved = await resolve_names_to_oracle_ids(engine, new_names)
                name_to_oracle.update(resolved)
                tried_names.update(n.lower() for n in new_names)
            rows = build_rows(page, name_to_oracle)
            # Drops = (pre-map v2 variant drops) + (unresolved card names)
            total_dropped += (raw_page_count - len(page)) + (len(page) - len(rows))
            if not rows:
                continue
            async with engine.begin() as conn:
                for i in range(0, len(rows), COMBOS_INSERT_BATCH):
                    await conn.execute(insert_stmt, rows[i : i + COMBOS_INSERT_BATCH])
            for r in rows:
                seen_ids.add(r["id"])
            total_written += len(rows)

    if not seen_ids:
        _log.warning(
            "combo ingest produced 0 rows (saw %d source combos, %d dropped)",
            total_seen, total_dropped,
        )
        return 0

    async with engine.begin() as conn:
        result = await conn.execute(
            text("DELETE FROM combos WHERE NOT (id = ANY(:seen))"),
            {"seen": sorted(seen_ids)},
        )
    deleted = result.rowcount if result.rowcount is not None else -1
    _log.info(
        "combo ingest wrote %d rows from %d source combos"
        " (dropped %d unresolved, deleted %d stale)",
        total_written, total_seen, total_dropped, deleted,
    )
    return total_written
