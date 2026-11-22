"""D10.16 — engine pool sizing.

The refresh runner fans out concurrent stages via `asyncio.gather` while
holding the advisory-lock connection. With SQLAlchemy's default
`pool_size=5` (plus a matching `max_overflow=10`), the branches
serialize on pool exhaustion once local traffic contends for the same
pool. Refresh engine bumps to a wider pool; read engine stays at the
defaults — request-serving traffic doesn't need it.
"""

from __future__ import annotations

from biblioplex.db import make_read_engine, make_refresh_engine

_DSN = "postgresql+psycopg://x/x"


def test_refresh_engine_pool_size_wider_than_read() -> None:
    read_pool = make_read_engine(_DSN).sync_engine.pool
    refresh_pool = make_refresh_engine(_DSN).sync_engine.pool
    # SQLAlchemy default pool_size is 5. Refresh engine must exceed it
    # so the advisory-lock conn + gather'd stage branches (embed,
    # classify, tags, combos, hnsw autocommit) don't serialize.
    assert refresh_pool.size() > read_pool.size()
    assert refresh_pool.size() >= 10


def test_read_statement_timeout_from_env(monkeypatch) -> None:
    import biblioplex.db as db
    from biblioplex.config import get_settings

    monkeypatch.setenv("DATABASE_URL", _DSN)
    assert get_settings().read_statement_timeout_ms == 5000
    monkeypatch.setenv("READ_STATEMENT_TIMEOUT_MS", "30000")
    ms = get_settings().read_statement_timeout_ms
    seen: dict = {}
    monkeypatch.setattr(db, "create_async_engine", lambda url, **kw: seen.update(kw))
    db.make_read_engine(_DSN, statement_timeout_ms=ms)
    assert "statement_timeout=30000 " in seen["connect_args"]["options"]
