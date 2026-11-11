"""Rebuild the ``pt_names`` table from Scryfall's ``all_cards`` bulk.

Portuguese printed names change only when a new set is released, and
``all_cards`` is ~10x the ``default_cards`` the refresh pipeline streams
— so this is a standalone, run-occasionally job, NOT a refresh stage.

    docker exec biblioplex-api-1 python scripts/build_pt_names.py
    # or, from the host venv against a reachable DATABASE_URL:
    make pt-names

Streams the bulk (gunzip + JSONL) through the shared ``iter_cards_async``
parser, keeps ``lang=pt`` rows via :func:`pt_pairs`, and replaces the
table contents in one transaction (TRUNCATE + batched INSERT). Idempotent
— safe to re-run; the table is rebuilt from scratch each time.
"""

from __future__ import annotations

import asyncio

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from biblioplex import SCRYFALL_HEADERS
from biblioplex.config import Settings
from biblioplex.ingest.bulk import discover_bulk_uri
from biblioplex.ingest.pt_names import INSERT_PT_SQL, TRUNCATE_PT_SQL, pt_pairs
from biblioplex.ingest.stream import iter_cards_async

_BULK_TYPE = "all_cards"
_BATCH = 1000


async def main() -> None:
    settings = Settings()
    engine = create_async_engine(settings.database_url)
    insert_stmt = text(INSERT_PT_SQL)
    total = 0
    batch: list[dict[str, str]] = []

    try:
        async with httpx.AsyncClient(timeout=None, headers=SCRYFALL_HEADERS) as client:
            download_uri = await discover_bulk_uri(client, _BULK_TYPE)
            print(f"[pt-names] streaming {_BULK_TYPE} from {download_uri}")

            async with engine.begin() as conn:
                await conn.execute(text(TRUNCATE_PT_SQL))
                async with client.stream("GET", download_uri) as response:
                    response.raise_for_status()
                    async for card in iter_cards_async(response.aiter_bytes()):
                        for printed_name, oracle_id in pt_pairs(card):
                            batch.append(
                                {"printed_name": printed_name, "oracle_id": oracle_id}
                            )
                        if len(batch) >= _BATCH:
                            await conn.execute(insert_stmt, batch)
                            total += len(batch)
                            batch = []
                            if total % (_BATCH * 20) == 0:
                                print(f"[pt-names] {total} rows staged")
                if batch:
                    await conn.execute(insert_stmt, batch)
                    total += len(batch)
        print(f"[pt-names] done — {total} pt printed-name rows")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
