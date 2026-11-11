"""Async SQLAlchemy engine factories.

Two engines per §9:

* **Read pool** — services every request. ``statement_timeout=5000`` ms is
  set via libpq ``options`` to bound runaway regex/ILIKE queries.
* **Refresh pool** — used by the bulk-ingest worker. No statement timeout,
  since the collapse step touches every row.

Neither engine is created at import time. Construction is deferred to the
FastAPI startup hook in :mod:`biblioplex.main` so tests and CLI tools can
import the package without a live database.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from biblioplex.dsl.compiler import NAME_WORD_SIMILARITY_THRESHOLD

READ_STATEMENT_TIMEOUT_MS = 5000


def make_read_engine(database_url: str) -> AsyncEngine:
    """Engine for request-serving code with a 5 s statement timeout.

    The `pg_trgm.word_similarity_threshold` GUC is set per-connection so
    the fuzzy OR-clause in the DSL name-substring predicate (D11.1) uses
    the same threshold the compiler emits its ``%>`` operator against,
    and so the `cards_name_trgm` GIN index is eligible to back it.
    """
    options = (
        f"-c statement_timeout={READ_STATEMENT_TIMEOUT_MS} "
        f"-c pg_trgm.word_similarity_threshold={NAME_WORD_SIMILARITY_THRESHOLD}"
    )
    return create_async_engine(
        database_url,
        connect_args={"options": options},
        pool_pre_ping=True,
    )


def make_refresh_engine(database_url: str) -> AsyncEngine:
    """Engine for the long-running refresh worker. No statement timeout.

    D10.16: pool sized for the refresh DAG. The runner holds the outer
    advisory-lock conn (1) then fans out `embed` / `classify` / `tags`
    / `combos` / `hnsw` (autocommit) plus the sub-conn combos ingest
    opens for name resolution — 6+ concurrent connections during a
    typical refresh. SQLAlchemy's default `pool_size=5` starves the
    branches; a `pool_size=10, max_overflow=10` cushion keeps the DAG
    truly parallel. Read engine stays at defaults — request-serving
    traffic is single-conn-per-request.
    """
    return create_async_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=10,
    )
