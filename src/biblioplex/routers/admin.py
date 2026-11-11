"""``POST /admin/refresh`` — tech_spec.md §6.5.

Triggers a bulk re-ingest. Returns immediately with a job id:

* ``{"status": "started", "job_id": "<uuid4>"}`` — claimed the run, the
  ingest is queued as a FastAPI ``BackgroundTask`` and will execute
  after the response is sent.
* ``{"status": "already_running", "job_id": "<existing>"}`` — another
  refresh is in flight; the returned ``job_id`` is that existing run's
  identifier so the caller can poll ``/health`` for it.

The 202 status code matches the "accepted, work pending" semantics. There
is no ``GET /admin/refresh/:job_id`` endpoint — ``/health`` reports the
most recent ``job_id`` and ``running`` flag.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel

from biblioplex.config import Settings
from biblioplex.embed.batch import embed_pending
from biblioplex.ingest.combos import ingest_combos_from_url
from biblioplex.ingest.refresh import (
    AlreadyRunning,
    RefreshRunner,
    RefreshStatus,
    default_classify,
    default_ingest,
)
from biblioplex.ingest.tags import ingest_oracle_tags


def build_refresh_runner(app: FastAPI) -> RefreshRunner:
    """Construct a RefreshRunner bound to the app's state.

    Shared by the HTTP endpoint dependency and the startup auto-refresh
    in `main.lifespan` so wiring stays in one place.
    """
    settings: Settings = app.state.settings
    model = getattr(app.state, "embed_model", None)

    async def _ingest(engine: Any) -> int:
        # Honor BULK_SOURCE_TYPE (§1.3): default_cards vs oracle_cards.
        return await default_ingest(engine, settings.bulk_source_type)

    async def _embed(engine: Any) -> int:
        if model is None:
            return 0
        return await embed_pending(engine, model, settings.embed_batch)

    async def _combos(engine: Any) -> int:
        # D14.1 — closure over the configured URL. Empty URL short-
        # circuits to 0 inside `ingest_combos_from_url` so a deployment
        # that doesn't want combos pays zero overhead per refresh.
        return await ingest_combos_from_url(engine, settings.combos_source_url)

    return RefreshRunner(
        engine=app.state.refresh_engine,
        status=app.state.refresh_status,
        ingest=_ingest,
        embed=_embed,
        tags=ingest_oracle_tags,
        classify=default_classify,
        combos=_combos,
    )


class RefreshResponse(BaseModel):
    """Body shape per §6.5."""

    status: str  # "started" | "already_running"
    job_id: str


def get_refresh_status(request: Request) -> RefreshStatus:
    """Dependency: the process-wide RefreshStatus singleton on app.state."""
    status: RefreshStatus = request.app.state.refresh_status
    return status


def get_refresh_runner(request: Request) -> RefreshRunner:
    """Dependency: a RefreshRunner bound to the app's refresh engine + status.

    Constructed per-request (cheap — it's a thin wrapper). Tests override
    this to inject fakes; production uses the default `default_ingest`
    pipeline body and binds `embed_pending` to the loaded SentenceTransformer
    + `EMBED_BATCH` from settings.
    """
    return build_refresh_runner(request.app)


router = APIRouter(prefix="/admin", tags=["admin"])


@router.post(
    "/refresh",
    response_model=RefreshResponse,
    status_code=202,
    summary="Trigger a bulk re-ingest",
    description=(
        "Claims the refresh slot if free and queues the ingest pipeline as a "
        "FastAPI background task. Returns immediately with the job id; the "
        "actual ingest (Scryfall download → stream parse → collapse → embed → "
        "HNSW build) runs after the response is sent. Two rapid POSTs do not "
        "race: an in-process compare-and-set plus `pg_advisory_lock(42)` "
        "ensure only one run is in flight per cluster."
    ),
    responses={
        202: {
            "description": (
                "Accepted. `status` is `started` (we claimed the slot) "
                "or `already_running` (someone else is mid-refresh)."
            ),
        },
    },
)
async def trigger_refresh(
    background_tasks: BackgroundTasks,
    stages: str | None = Query(
        None,
        description=(
            "Optional comma-separated subset of stages to run. Omit or "
            "leave empty to run the full pipeline. Valid names: "
            "`ingest`, `embed`, `classify`, `hnsw`, `tags`, `combos`, `deck_cards`."
        ),
    ),
    status: RefreshStatus = Depends(get_refresh_status),
    runner: RefreshRunner = Depends(get_refresh_runner),
) -> RefreshResponse:
    """Claim the refresh slot if free; queue the ingest as a background task.

    Atomicity: :meth:`RefreshStatus.try_mark_started` is the
    compare-and-set point — if it returns the existing job_id, we never
    schedule the background task, so two rapid POSTs can't both kick off
    runs in the same process.
    """
    selected: frozenset[str] | None = None
    if stages:
        parts = frozenset(s.strip() for s in stages.split(",") if s.strip())
        unknown = parts - set(RefreshRunner.ALL_STAGES)
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=f"unknown stages: {sorted(unknown)}; "
                       f"valid: {list(RefreshRunner.ALL_STAGES)}",
            )
        if parts:
            selected = parts

    candidate_job_id = str(uuid.uuid4())
    existing = status.try_mark_started(candidate_job_id)
    if existing is not None:
        # Another run is in flight; do NOT schedule a duplicate.
        return RefreshResponse(status="already_running", job_id=existing)

    # We hold the in-process slot. The advisory lock check happens inside
    # `runner.run_locked()` and may still raise AlreadyRunning if another
    # replica beat us to it — `run_locked` will release the in-process
    # status flag via its `finally` either way.
    async def _background() -> None:
        try:
            await runner.run_locked(selected)
        except AlreadyRunning:
            # Cross-process race: another replica had the advisory lock.
            # Status was already reset by run_locked's finally. Nothing
            # else to do; the failed attempt is logged at the runner.
            return
        except Exception:
            # Any other failure also leaves status reset thanks to the
            # runner's finally. Re-raise so FastAPI's background-task
            # error logging picks it up.
            raise

    background_tasks.add_task(_background)
    return RefreshResponse(status="started", job_id=candidate_job_id)
