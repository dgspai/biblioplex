"""``GET /health`` per tech_spec.md §6.6.

Always returns 200 when the DB ping (``SELECT 1``) succeeds. Returns 503
only when the ping itself fails — refresh state never flips the status,
orchestrators read ``refresh.running`` to decide policy.

Refresh state is sourced from the process-local
:class:`biblioplex.ingest.refresh.RefreshStatus` singleton stored on
``app.state.refresh_status`` (wired in M1 D1.7). The M0 placeholder is
gone — `running`, `started_at`, and `job_id` now reflect real ingest
worker state.

The optional ``oracle_tags_count`` and ``combos_count`` fields use
``pg_class.reltuples`` estimates rather than ``count(*)`` — the probe
must not do a full scan on a table that may be large or missing. A
value of ``-1`` (pre-``ANALYZE``) or a missing row (table absent on
pre-migration deploys) both surface as ``None``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Protocol

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from biblioplex import __version__
from biblioplex.ingest.refresh import RefreshStatus

_log = logging.getLogger(__name__)


async def _table_estimate(
    conn: Any, table: str, *, probe_when_unanalysed: bool = False
) -> int | None:
    """Return ``pg_class.reltuples`` estimate for ``table`` or ``None``.

    Fast O(1) catalog lookup, never full-scans. Missing table (row
    absent) or pre-``ANALYZE`` (reltuples = -1) both yield ``None``.
    On any error, roll back the aborted sub-txn so subsequent probes
    on the same connection still work, then return ``None`` — the
    estimate is not part of the readiness contract.

    ``probe_when_unanalysed``: reltuples is -1 until the first
    ANALYZE/autovacuum, so a freshly created or freshly loaded table reads
    as unknown. Fall back to a count capped at 1000 rows (O(1000), never a
    full scan): 0 means truly empty, 1000 means "1000 or more".
    """
    try:
        result = await conn.execute(
            text("SELECT reltuples::bigint FROM pg_class WHERE relname = :t"),
            {"t": table},
        )
        row = result.scalar_one_or_none()
    except Exception as exc:
        _log.warning("%s estimate failed; returning null: %s", table, exc)
        await conn.rollback()
        return None
    if row is not None and row < 0 and probe_when_unanalysed:
        try:
            floor = await conn.execute(
                text(f"SELECT count(*) FROM (SELECT 1 FROM {table} LIMIT 1000) s")  # noqa: S608
            )
            return int(floor.scalar_one())
        except Exception as exc:
            _log.warning("%s floor probe failed; returning null: %s", table, exc)
            await conn.rollback()
            return None
    if row is None or row < 0:
        return None
    return int(row)


class _Engine(Protocol):
    """Subset of :class:`AsyncEngine` we actually use here.

    Defined locally so the dependency can be substituted with a fake in
    tests without dragging the SQLAlchemy class hierarchy along.
    """

    def connect(self) -> object: ...


def get_read_engine(request: Request) -> _Engine:
    """FastAPI dependency: read-pool engine bound to the app state."""
    engine: _Engine = request.app.state.read_engine
    return engine


def get_refresh_status(request: Request) -> RefreshStatus:
    """FastAPI dependency: refresh-status singleton from app.state.

    Duplicated alongside the same dependency in :mod:`routers.admin` so
    /health doesn't have to import the admin router (avoids a cycle and
    keeps the two endpoints loosely coupled).
    """
    status: RefreshStatus = request.app.state.refresh_status
    return status


class StageBody(BaseModel):
    """Per-stage entry inside ``refresh.stages``.

    - ``finished_at`` is ``null`` while a stage is in flight so callers
      can tell "which stage is currently running" without a separate
      field.
    - ``rows`` is what the stage body reported (``ingest``, ``embed``,
      ``classify``, ``tags``, ``combos``); ``null`` for ``hnsw`` and for
      stages that raised before setting it.
    - ``error`` is ``null`` for a clean run and ``"<ExcType>: <msg>"``
      when the stage body raised — lets callers distinguish "ran but
      crashed" from "ran clean" without log-scraping.
    """

    name: str
    started_at: datetime
    finished_at: datetime | None = None
    rows: int | None = None
    error: str | None = None


class RefreshStatusBody(BaseModel):
    """§6.6 ``refresh`` sub-object — serialized RefreshStatus snapshot."""

    running: bool = False
    started_at: datetime | None = None
    job_id: str | None = None
    stages: list[StageBody] = []
    #: Stages the current/last run selected, in execution order.
    planned_stages: list[str] = []


class HealthResponse(BaseModel):
    """Body shape per §6.6."""

    status: str
    db: str
    #: Running Biblioplex version; static, so present even when the DB is down.
    version: str = __version__
    cards_count: int | None
    oracle_tags_count: int | None
    embeddings_count: int | None
    combos_count: int | None
    refresh: RefreshStatusBody


router = APIRouter(tags=["health"])


@router.get(
    "/health/live",
    summary="Liveness probe",
    description=(
        "Sub-second liveness probe: fires `SELECT 1` and returns "
        "`{status: 'ok'}` on success, 503 `{status: 'error'}` on any "
        "DB error. Used for k8s liveness probes that must not block on "
        "reltuples-side stats or refresh-state locks — that's `/health`'s "
        "job."
    ),
)
async def check_liveness(
    engine: _Engine = Depends(get_read_engine),
) -> JSONResponse:
    """Sub-second SELECT-1 liveness."""
    try:
        async with engine.connect() as conn:  # type: ignore[attr-defined]
            await conn.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse(status_code=503, content={"status": "error"})
    return JSONResponse(status_code=200, content={"status": "ok"})


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness + readiness probe",
    description=(
        "Pings the DB, reads `count(*) FROM cards`, and reports refresh "
        "state. Returns 200 when `SELECT 1` succeeds (even mid-refresh); "
        "503 only when the DB ping itself fails. Orchestrators read "
        "`refresh.running` from the body and decide policy themselves."
    ),
    responses={
        503: {
            "description": "DB ping failed; `db` is `down`.",
            "model": HealthResponse,
        },
    },
)
async def health(
    engine: _Engine = Depends(get_read_engine),
    status: RefreshStatus = Depends(get_refresh_status),
) -> JSONResponse:
    """Ping DB, read ``count(*) FROM cards``, return §6.6 body with refresh state."""
    snapshot = status.snapshot()
    refresh = RefreshStatusBody(
        running=snapshot.running,
        started_at=snapshot.started_at,
        job_id=snapshot.job_id,
        planned_stages=list(snapshot.planned),
        stages=[
            StageBody(
                name=s.name,
                started_at=s.started_at,
                finished_at=s.finished_at,
                rows=s.rows,
                error=s.error,
            )
            for s in snapshot.stages
        ],
    )

    try:
        async with engine.connect() as conn:  # type: ignore[attr-defined]
            await conn.execute(text("SELECT 1"))
            # D16.4: cards_count now uses the same pg_class.reltuples
            # estimate path as tags/combos — the prior `count(*) FROM cards`
            # (with `count(*) FILTER (WHERE embedding IS NOT NULL)`) was
            # a full sequential scan on a growing table on every readiness
            # probe. embeddings_count is dropped from the reltuples path
            # (agents rarely poll it; the FILTER would force a full scan).
            cards_count = await _table_estimate(
                conn, "cards", probe_when_unanalysed=True
            )
            # D16.8: recover a live embeddings count from the last
            # completed `embed` stage's reported rows. Cheap — no DB
            # touch; the stage telemetry is already populated by the
            # refresh runner. `None` when no refresh has completed
            # yet or when a subset run skipped the embed stage.
            embeddings_count: int | None = next(
                (s.rows for s in snapshot.stages if s.name == "embed"), None
            )
            oracle_tags_count = await _table_estimate(conn, "oracle_tags")
            combos_count = await _table_estimate(conn, "combos")
    except Exception:
        body = HealthResponse(
            status="error",
            db="down",
            cards_count=None,
            oracle_tags_count=None,
            embeddings_count=None,
            combos_count=None,
            refresh=refresh,
        )
        return JSONResponse(status_code=503, content=body.model_dump(mode="json"))

    body = HealthResponse(
        status="ok",
        db="up",
        cards_count=cards_count,
        oracle_tags_count=oracle_tags_count,
        embeddings_count=embeddings_count,
        combos_count=combos_count,
        refresh=refresh,
    )
    return JSONResponse(status_code=200, content=body.model_dump(mode="json"))
