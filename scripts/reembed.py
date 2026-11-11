"""Re-embed every card. Use after changing the embed_source expression
or HF_EMBED_MODEL — bypasses the Scryfall download in /admin/refresh.

Run inside the api container so the SentenceTransformer cache is hot:
    docker exec biblioplex-api-1 python scripts/reembed.py
"""

from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from biblioplex.config import Settings
from biblioplex.embed.batch import embed_pending
from biblioplex.embed.model import load_model


async def main() -> None:
    settings = Settings()
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text("UPDATE cards SET embedding = NULL"))
            print(f"[reembed] cleared {result.rowcount} embeddings")
        model = load_model(settings)
        n = await embed_pending(engine, model, settings.embed_batch)
        print(f"[reembed] wrote {n} embeddings")
        async with engine.begin() as conn:
            await conn.execute(text(
                "DROP INDEX IF EXISTS cards_embedding_hnsw; "
                "CREATE INDEX cards_embedding_hnsw "
                "ON cards USING hnsw (embedding vector_cosine_ops);"
            ))
        print("[reembed] HNSW index rebuilt")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
