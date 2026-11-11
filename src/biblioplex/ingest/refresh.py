"""Refresh runner — orchestrates the §4.2 ingest flow.

Sequence:

1. Acquire ``pg_try_advisory_lock(42)`` on a dedicated connection. If the
   lock is already held by another worker, raise :class:`AlreadyRunning`
   so the HTTP layer can return ``{"status":"already_running"}``.
2. Flip :class:`RefreshStatus` to ``running=True`` with the caller's
   ``job_id`` (UUID4 minted by the endpoint, D1.7).
3. Run the ingest function (default: :func:`default_ingest`, which wires
   D1.1 discovery → D1.2 stream → D1.3 split → D1.4 flatten → batched
   staging INSERT).
4. Run :data:`COLLAPSE_SQL` per §4.2 step 4 to pivot the freshly-staged
   rows into ``cards`` (``DISTINCT ON (oracle_id)`` keeps the oldest
   (first) printing per oracle).
5. Run :data:`TRUNCATE_STAGING_SQL` per §4.2 step 7 to leave the table
   empty for the next run.
6. Release the lock and flip the status back to ``running=False`` in a
   ``finally`` block so a failing ingest doesn't strand the lock.

The runner is dependency-injectable: tests pass a fake engine plus a
stubbed ingest callable. Production wiring (D1.7) supplies the refresh
engine and the real pipeline.

State is process-local — refresh status lives in the FastAPI app state,
not in Postgres. The advisory lock handles cross-process exclusion; this
in-process status object handles single-process queueing and exposes
telemetry to ``/health``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, cast

import httpx
from psycopg.types.json import Jsonb
from sqlalchemy import text

from biblioplex import SCRYFALL_BULK_TIMEOUT, SCRYFALL_HEADERS
from biblioplex.classify.rules import LABELS, classify_by_rules_ex
from biblioplex.decks.service import rebuild_all_deck_cards
from biblioplex.ingest.bulk import discover_bulk_uri
from biblioplex.ingest.collapse import (
    COLLAPSE_SQL,
    PRINTINGS_SQL,
    STAGING_COLUMNS,
    TRUNCATE_STAGING_SQL,
)
from biblioplex.ingest.color_sort import color_sort_key
from biblioplex.ingest.flatten import flatten_card
from biblioplex.ingest.reversible import REVERSIBLE_LAYOUT, split_reversible
from biblioplex.ingest.stream import iter_cards_async

# psycopg3 won't auto-adapt Python dict/list → jsonb in `%s` parameters;
# wrap these columns explicitly before executemany.
_JSONB_COLUMNS: frozenset[str] = frozenset({"legalities", "card_faces", "all_parts", "raw"})

_log = logging.getLogger(__name__)

# §4.2 step 3 — batch size for staging INSERTs. 256 matches `EMBED_BATCH`'s
# default so memory budgets are aligned across the pipeline; the env knob
# stays embed-only for now (introducing a separate INGEST_BATCH is YAGNI
# until a profiling signal demands it).
INGEST_BATCH: int = 256

# §4.2 step 1 — the lock key. Constant across the cluster so all replicas
# agree on who's allowed to refresh.
ADVISORY_LOCK_KEY: int = 42

# §4.2 step 6 — HNSW index rebuild after embeddings populated. D13.7
# splits this into three statements so the rebuild runs CONCURRENTLY on
# a scratch index name, leaving the currently-serving
# `cards_embedding_hnsw` in place for `/similar` throughout the build.
# A tiny swap txn drops the old and renames the new — the only window
# where the two coexist is inside that txn (ms-level).
#
# ponytail: drop+rebuild on every refresh; cheaper than incremental for
# our volume per §4.2 step 6 (bulk-then-index is ~10× faster than
# incremental for a few-hundred-thousand-row table).

# Pre-cleanup — handles the case where a prior refresh crashed mid-
# CONCURRENTLY and left an INVALID `cards_embedding_hnsw_new` around.
# Safe on a clean slate (IF EXISTS is a no-op then).
HNSW_CONCURRENT_CLEANUP_SQL: str = (
    "DROP INDEX IF EXISTS cards_embedding_hnsw_new;"
)

# Concurrent build under a scratch name. CONCURRENTLY forbids running
# inside a transaction, so the runner opens a separate AUTOCOMMIT
# connection for this call (see `_rebuild_hnsw_concurrent`). pgvector
# has supported HNSW under CONCURRENTLY since 0.5.0.
HNSW_CONCURRENT_BUILD_SQL: str = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS cards_embedding_hnsw_new "
    "ON cards USING hnsw (embedding vector_cosine_ops);"
)

# Atomic swap. Both statements run in the same tiny txn on the lock-
# holding connection so no query ever sees "both indexes" or "no index".
HNSW_SWAP_SQL: str = (
    "DROP INDEX IF EXISTS cards_embedding_hnsw; "
    "ALTER INDEX cards_embedding_hnsw_new RENAME TO cards_embedding_hnsw;"
)


class AlreadyRunning(RuntimeError):
    """Raised when a refresh is already in flight (lock or status conflict)."""


@dataclass(frozen=True)
class StageSnapshot:
    """Per-stage record produced by :meth:`RefreshStatus.stage`.

    - ``finished_at`` is ``None`` while the stage is in flight (the
      current stage of a running refresh) and is filled in by the
      context-manager exit — success or failure — so a crashed stage
      still surfaces its duration in ``/health``.
    - ``rows`` is the count reported by the stage body via
      :attr:`StageHandle.rows`. ``None`` for stages that don't have a
      row concept (``hnsw``) or that were interrupted before setting it.
    - ``error`` is ``None`` for a clean run and the exception's
      ``"<Type>: <msg>"`` string when the stage body raised, letting
      callers distinguish "ran but crashed" from "ran clean" without
      log-scraping.
    """

    name: str
    started_at: datetime
    finished_at: datetime | None
    rows: int | None = None
    error: str | None = None


class StageHandle:
    """Yielded by :meth:`RefreshStatus.stage`; lets the body report ``rows``.

    Thin proxy over the underlying mutable stage entry — writes take the
    status lock so a concurrent :meth:`RefreshStatus.snapshot` can't
    observe a torn read. Kept as a class (not a namedtuple) so the
    ``rows`` setter can guard the lock.
    """

    def __init__(self, entry: dict[str, Any], lock: threading.Lock) -> None:
        self._entry = entry
        self._lock = lock

    @property
    def rows(self) -> int | None:
        with self._lock:
            value = self._entry["rows"]
        # D10.12: setter is type-hinted `int | None` and is the only
        # writer; cast narrows the dict lookup without a runtime assert
        # (which would silently drop under `python -O`).
        return cast("int | None", value)

    @rows.setter
    def rows(self, value: int | None) -> None:
        with self._lock:
            self._entry["rows"] = value


@dataclass(frozen=True)
class RefreshSnapshot:
    """Immutable view of the current refresh state — what /health reads."""

    running: bool
    started_at: datetime | None
    job_id: str | None
    stages: tuple[StageSnapshot, ...] = ()
    #: Stage names the current/last run selected, in execution order.
    #: Lets a UI show "N of M" before the later stages have started.
    planned: tuple[str, ...] = ()


class RefreshStatus:
    """In-process telemetry for the refresh worker.

    Thread-safe (uses a ``threading.Lock``) so the snapshot reads from
    ``/health`` don't race the mark_started/mark_finished writes from the
    background task. Stays purely in memory — the cross-process exclusion
    guarantee comes from the Postgres advisory lock, not from this object.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running: bool = False
        self._started_at: datetime | None = None
        self._job_id: str | None = None
        # D13.5 — monotonically increasing counter bumped on every
        # mark_finished() call. Search-response ETags fold this in so
        # every completed refresh invalidates cached search results.
        # Callers snapshot via `epoch()`. Seeded from the boot time, not 0,
        # so a restarted process never re-issues ETags a browser cached from
        # the previous one (a fresh DB otherwise 304s into stale results).
        self._epoch: int = time.time_ns()
        # Per-stage timing for /health telemetry. Reset on every new run
        # (mark_started / try_mark_started); preserved across the
        # subsequent mark_finished so the last run's stage timings stay
        # visible until a new run starts. Each entry is a mutable dict
        # while the stage is in flight — the finish path mutates
        # `finished_at` in place, which is why snapshots copy into
        # frozen StageSnapshots on read.
        self._stages: list[dict[str, Any]] = []
        self._planned: tuple[str, ...] = ()

    def set_planned(self, names: tuple[str, ...]) -> None:
        with self._lock:
            self._planned = names

    def snapshot(self) -> RefreshSnapshot:
        """Return an immutable view of the current state."""
        with self._lock:
            return RefreshSnapshot(
                running=self._running,
                started_at=self._started_at,
                job_id=self._job_id,
                planned=self._planned,
                stages=tuple(
                    StageSnapshot(
                        name=s["name"],
                        started_at=s["started_at"],
                        finished_at=s["finished_at"],
                        rows=s["rows"],
                        error=s["error"],
                    )
                    for s in self._stages
                ),
            )

    def mark_started(self, job_id: str) -> None:
        """Flip to running. Raises :class:`AlreadyRunning` if already running."""
        with self._lock:
            if self._running:
                raise AlreadyRunning(
                    f"refresh already in flight (job_id={self._job_id})"
                )
            self._running = True
            self._started_at = datetime.now(tz=UTC)
            self._job_id = job_id
            self._stages = []
            self._planned = ()

    def try_mark_started(self, job_id: str) -> str | None:
        """Atomic compare-and-set used by the HTTP endpoint (D1.7).

        Returns:
            ``None`` if the caller successfully claimed the run (status is
            now ``running=True`` with the supplied ``job_id``).
            The existing in-flight ``job_id`` if another run is already
            mid-flight — caller returns ``{"status":"already_running"}``
            with that id.
        """
        with self._lock:
            if self._running:
                return self._job_id
            self._running = True
            self._started_at = datetime.now(tz=UTC)
            self._job_id = job_id
            self._stages = []
            self._planned = ()
            return None

    @contextmanager
    def stage(self, name: str) -> Iterator[StageHandle]:
        """Record start/end + rows + error around a refresh stage.

        Yields a :class:`StageHandle` the body assigns ``rows`` on; on
        exception the exception's ``"<Type>: <msg>"`` is recorded as
        ``error`` and re-raised so callers still see the failure
        (best-effort stages wrap this in their own outer try/except).
        ``finished_at`` is stamped in the ``finally`` block so a crashed
        stage still surfaces wall-clock duration.

        Emits one INFO log line on entry and one on exit (success or
        failure) so every step is observable end-to-end from the log
        stream without needing to poll ``/health``.
        """
        with self._lock:
            entry: dict[str, Any] = {
                "name": name,
                "started_at": datetime.now(tz=UTC),
                "finished_at": None,
                "rows": None,
                "error": None,
            }
            self._stages.append(entry)
        _log.info("refresh stage '%s' starting", name)
        handle = StageHandle(entry, self._lock)
        try:
            yield handle
        except Exception as exc:
            with self._lock:
                entry["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            with self._lock:
                entry["finished_at"] = datetime.now(tz=UTC)
                elapsed = (entry["finished_at"] - entry["started_at"]).total_seconds()
                rows = entry["rows"]
                err = entry["error"]
            if err:
                _log.info("refresh stage '%s' FAILED after %.2fs: %s", name, elapsed, err)
            elif rows is not None:
                _log.info("refresh stage '%s' done in %.2fs (rows=%d)", name, elapsed, rows)
            else:
                _log.info("refresh stage '%s' done in %.2fs", name, elapsed)

    def mark_finished(self, *, bump_epoch: bool = True) -> None:
        """Flip back to idle. Retains ``job_id`` for /health visibility.

        ``bump_epoch=False`` gates the D13.5 epoch increment (Bug.6). Used
        by the ``AlreadyRunning`` bail path in :meth:`RefreshRunner.run_locked`
        so a losing replica doesn't invalidate every search-response ETag
        cache for a refresh that never actually ran. The winning replica
        bumps epoch when IT completes — one bump per real completion.
        """
        with self._lock:
            self._running = False
            # D13.5 — bump epoch so downstream consumers (search-response
            # ETag) can detect that a refresh completed and invalidate
            # cached results. Bump on EVERY real finish (success or
            # failure) because a failed refresh can still have committed
            # the ingest+collapse txn — see refresh runner's split-txn
            # design. Skip when a run bailed before any DB write (Bug.6).
            if bump_epoch:
                self._epoch += 1
            # started_at and job_id deliberately preserved so /health can
            # report "last run was X with id Y"; the next mark_started
            # overwrites them.

    def epoch(self) -> int:
        """Snapshot the monotonic refresh epoch (D13.5).

        Bumped on every :meth:`mark_finished` call. Search-response
        ETags fold it in so a completed refresh invalidates cached
        result sets.
        """
        with self._lock:
            return self._epoch


class _Engine(Protocol):
    """Minimal subset of SQLAlchemy ``AsyncEngine`` the runner touches."""

    def begin(self) -> Any: ...

    def connect(self) -> Any: ...


IngestFn = Callable[[_Engine], Awaitable[int]]
"""Signature for the ingest body. Returns the number of staged rows.

Wired to :func:`default_ingest` in production. Tests inject stubs.
"""


EmbedFn = Callable[[_Engine], Awaitable[int]]
"""Signature for the embed body. Returns the number of rows embedded.

Wired to :func:`default_embed` in production (which closes over the
loaded model + batch size). Tests inject stubs.
"""


async def _noop_embed(_engine: _Engine) -> int:
    """Default embed callable — no-op when no model/batch is wired.

    Lets unit tests construct a :class:`RefreshRunner` without dragging
    in the embedding subsystem. Production wiring overrides this with
    a partial bound to the loaded :class:`SentenceTransformer`.
    """
    return 0


TagIngestFn = Callable[[_Engine], Awaitable[int]]
"""Signature for the oracle-tag ingest body. Returns rows written.

Wired to :func:`ingest_oracle_tags` in production; tests inject a stub
so they don't hit Scryfall (per-test network deadlines).
"""


async def _noop_tags(_engine: _Engine) -> int:
    """Default tag-ingest callable — no-op when caller doesn't want tags."""
    return 0


CombosIngestFn = Callable[[_Engine], Awaitable[int]]
#: D19.24 — rebuilds the derived `deck_cards` index. Returns decks rebuilt.
DeckCardsFn = Callable[[_Engine], Awaitable[int]]
"""Signature for the combo-ingest body. Returns rows written.

Wired to a closure over :func:`biblioplex.ingest.combos.ingest_combos_from_url`
bound to ``settings.combos_source_url`` in production (D14.1); tests
inject stubs. When the URL is empty the closure is a no-op.
"""


async def _noop_combos(_engine: _Engine) -> int:
    """Default combos-ingest callable — no-op when combos source is disabled."""
    return 0


ClassifyFn = Callable[[_Engine], Awaitable[int]]
"""Signature for the post-embed rules-classify body. Returns rows scored.

Wired to :func:`default_classify` in production; tests inject a stub
so the runner can be exercised without the rules module + DB roundtrip.
"""


# template-classifier-plan.md Step 5. Page size chosen to bound the
# Python-side working set; classify_by_rules is pure regex over
# oracle_text so the bottleneck is the round-trip, not the work.
_CLASSIFY_PAGE: int = 2048

_CLASSIFY_SELECT_SQL: str = (
    "SELECT id, oracle_text, card_faces, label_reasons FROM cards "
    "WHERE id > :last_id "
    "ORDER BY id LIMIT :limit"
)
_CLASSIFY_UPDATE_SQL: str = (
    "UPDATE cards SET predicted_labels = :labels, label_scores = :scores, "
    "label_reasons = :reasons WHERE id = :id"
)


async def _noop_classify(_engine: _Engine) -> int:
    """Default classify callable — no-op when caller doesn't want predictions."""
    return 0


async def default_classify(engine: _Engine) -> int:
    """Score every card via :func:`classify_by_rules` and persist verdicts.

    Reads ``(id, oracle_text, card_faces)`` in pages, runs the rules-layer
    classifier, and writes ``predicted_labels`` (labels with score >= 0.5)
    + ``label_scores`` (full ``{label: score}`` map) back via
    ``executemany``. One transaction per page so a mid-flight failure
    leaves a partial-but-consistent state rather than rolling the whole
    classify back.

    Multi-faced cards are handled inside ``classify_by_rules`` — every
    face's ``oracle_text`` is scored and the per-label result is unioned
    via ``max`` per R25.
    """
    threshold = 0.5
    select_stmt = text(_CLASSIFY_SELECT_SQL)
    update_stmt = text(_CLASSIFY_UPDATE_SQL)
    total = 0
    # D15.10: keyset pagination — threads `max(id)` from the prior page.
    # Bootstraps with the zero UUID so the first page fetches from row 1.
    last_id: str = "00000000-0000-0000-0000-000000000000"
    page = 0
    while True:
        async with engine.connect() as conn, conn.begin():
            rows = (
                await conn.execute(
                    select_stmt, {"limit": _CLASSIFY_PAGE, "last_id": last_id}
                )
            ).all()
            if not rows:
                return total
            payload: list[dict[str, Any]] = []
            for row in rows:
                scores, reasons = classify_by_rules_ex({
                    "oracle_text": row.oracle_text,
                    "card_faces": row.card_faces,
                })
                # A manual label override (set via PUT /cards/:id/labels and
                # stored under the reserved `_override` key of label_reasons)
                # wins over the rules verdict and is carried forward, so a
                # full re-classify never silently wipes a human correction.
                old = row.label_reasons if isinstance(row.label_reasons, dict) else {}
                override = old.get("_override")
                if isinstance(override, list):
                    labels = list(override)
                    reasons["_override"] = override
                else:
                    labels = [lab for lab in LABELS if scores[lab] >= threshold]
                payload.append({
                    "id": str(row.id),
                    "labels": labels,
                    "scores": Jsonb(scores),
                    "reasons": Jsonb(reasons),
                })
            await conn.execute(update_stmt, payload)
            total += len(payload)
            # Query ordered by id ASC — the last row is the max.
            last_id = str(rows[-1].id)
        page += 1
        if page % 5 == 0:
            _log.info("classify: %d rows scored", total)


def _build_insert_sql() -> str:
    """Render the parameterized staging INSERT once at import time."""
    cols = ", ".join(STAGING_COLUMNS)
    binds = ", ".join(f":{c}" for c in STAGING_COLUMNS)
    return f"INSERT INTO cards_staging ({cols}) VALUES ({binds})"  # noqa: S608


_STAGING_INSERT_SQL: str = _build_insert_sql()


def _parse_usd(value: Any) -> Decimal | None:
    """Coerce a Scryfall ``prices.usd`` field to a :class:`Decimal` or ``None``.

    Scryfall ships USD prices as nullable decimal strings (``"0.42"``,
    ``"1.23"``, ``null``). Empty strings and obviously non-numeric strings
    (``"n/a"``) are also tolerated and decay to ``None`` — refresh must
    never crash on a malformed price row, and ``None`` lets the order=usd
    sort sink those cards under ``NULLS LAST`` per the matching index.

    Args:
        value: The raw ``prices.usd`` field (or any value).

    Returns:
        ``Decimal(value)`` when ``value`` is a non-empty string that parses
        cleanly; ``None`` for ``None``, empty string, or any
        :class:`InvalidOperation`.
    """
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _to_staging_row(card: dict[str, Any]) -> dict[str, Any]:
    """Flatten a card then project to the staging column set.

    Missing keys map to ``None`` so the executemany payload shape stays
    uniform across cards regardless of which optional Scryfall fields a
    given printing populates.

    D1.9 additions:
    * ``edhrec_rank`` — top-level int|None on Scryfall; passed through verbatim.
    * ``price_usd`` — parsed from ``prices.usd`` via :func:`_parse_usd`. The
      collapse step replaces this with a per-oracle ``MIN(...) OVER`` window,
      so the value staged here is just the printing's own price; the cheapest
      one wins at collapse time regardless of which row DISTINCT ON picks.
    """
    flat = flatten_card(card)
    row: dict[str, Any] = {col: flat.get(col) for col in STAGING_COLUMNS}
    # `raw` carries the full original card JSON for projection fallback
    # (§7). Use the pre-flatten dict so we don't pollute it with our
    # derived `_search` columns.
    row["raw"] = card
    # D1.9: rank passes through; price needs Decimal coercion.
    row["edhrec_rank"] = card.get("edhrec_rank")
    prices = card.get("prices") or {}
    row["price_usd"] = _parse_usd(prices.get("usd"))
    # §6.1: canonical WUBRG-cycle position for order=color.
    row["color_sort_key"] = color_sort_key(flat.get("colors_eff"))
    for col in _JSONB_COLUMNS:
        if row[col] is not None:
            row[col] = Jsonb(row[col])
    return row


def _expand_card(card: dict[str, Any]) -> list[dict[str, Any]]:
    """Stream-parser output → one or more staging-ready rows.

    Reversible parents split into one row per face (§3.4); every other
    layout passes through as a single row. The drop filter for
    tokens/emblems/etc. lives in :func:`iter_cards` upstream.
    """
    if card.get("layout") == REVERSIBLE_LAYOUT:
        return [_to_staging_row(face) for face in split_reversible(card)]
    return [_to_staging_row(card)]


async def default_ingest(engine: _Engine, bulk_type: str = "default_cards") -> int:
    """Run the real M1 ingest pipeline end-to-end.

    Chains §4.2 steps 1–3:

    1. Resolve the current ``bulk_type`` download URI via D1.1.
    2. Open an ``httpx`` GET stream against that URI.
    3. ``iter_cards_async`` gunzips and parses the JSONL stream
       incrementally, applying the §4.2 drop filter (Bug.34).
    4. Each kept card (D1.2 drop filter) flows through D1.3 reversible
       split and D1.4 face-flatten.
    5. Rows are batched into ``cards_staging`` with one parameterized
       ``executemany`` per :data:`INGEST_BATCH` (256) rows.

    Collapse (§4.2 step 4) and truncate (step 7) are the runner's job;
    this function returns once staging is fully populated.

    Args:
        engine: SQLAlchemy async engine whose ``begin()`` yields a
            transaction-scoped connection.
        bulk_type: Scryfall bulk-data slug to ingest (``default_cards`` or
            ``oracle_cards``). Bound to ``settings.bulk_source_type`` in
            production; the default keeps standalone callers on §1.3 default.

    Returns:
        Total number of rows written to ``cards_staging``.
    """
    total = 0
    batch: list[dict[str, Any]] = []
    insert_stmt = text(_STAGING_INSERT_SQL)
    # ~40k rows per log line at INGEST_BATCH=256 → 5-10 progress lines
    # across a full Scryfall bulk. ponytail: constant, bump if too chatty.
    progress_every_rows = INGEST_BATCH * 160
    next_progress = progress_every_rows

    async with httpx.AsyncClient(timeout=SCRYFALL_BULK_TIMEOUT, headers=SCRYFALL_HEADERS) as client:
        download_uri = await discover_bulk_uri(client, bulk_type)
        _log.info("ingest: streaming %s from %s", bulk_type, download_uri)

        async with engine.begin() as conn:
            async with client.stream("GET", download_uri) as response:
                response.raise_for_status()

                # iter_cards_async owns gunzip + JSONL split + drop filter.
                async for card in iter_cards_async(response.aiter_bytes()):
                    for row in _expand_card(card):
                        batch.append(row)
                        if len(batch) >= INGEST_BATCH:
                            await conn.execute(insert_stmt, batch)
                            total += len(batch)
                            batch = []
                            if total >= next_progress:
                                _log.info("ingest: %d rows staged", total)
                                next_progress += progress_every_rows

            if batch:
                await conn.execute(insert_stmt, batch)
                total += len(batch)
                batch = []

    return total


async def default_deck_cards(engine: _Engine) -> int:
    """Regenerate the derived `deck_cards` index for every stored deck.

    Two jobs in one pass: BACKFILL for decks that predate D19.19 (they
    carry no rows at all, so coverage sorting ranks them last forever and
    their card table comes back empty), and REPAIR of resolution drift —
    a name that matched nothing before this refresh may match now, and
    coverage silently under-reports until the row is rewritten.

    Runs post-`ingest` because resolution reads `cards`. Cheap relative to
    the rest of a refresh: O(decks), each one DELETE plus one
    INSERT…SELECT, against a table that holds tens of rows, not millions.

    `engine.begin()`, never `connect()` — D18.3: `connect()` opens an
    implicit transaction that is ROLLED BACK on close, so the rebuild
    would write nothing and report success.
    """
    async with engine.begin() as conn:
        return await rebuild_all_deck_cards(conn)


class RefreshRunner:
    """Drives one refresh cycle end-to-end with the advisory-lock gate."""

    def __init__(
        self,
        engine: _Engine,
        status: RefreshStatus,
        ingest: IngestFn = default_ingest,
        embed: EmbedFn = _noop_embed,
        tags: TagIngestFn = _noop_tags,
        classify: ClassifyFn = _noop_classify,
        combos: CombosIngestFn = _noop_combos,
        deck_cards: DeckCardsFn = default_deck_cards,
    ) -> None:
        self._engine = engine
        self._status = status
        self._ingest = ingest
        self._embed = embed
        self._tags = tags
        self._classify = classify
        self._combos = combos
        self._deck_cards = deck_cards

    async def run(self, job_id: str) -> int:
        """Mark in-process status, then call :meth:`run_locked`.

        This is the convenience entrypoint for standalone callers (tests,
        future CLI). The HTTP endpoint (D1.7) instead calls
        :meth:`RefreshStatus.try_mark_started` itself and then
        :meth:`run_locked` from the background task, so the response can
        be returned BEFORE any DB roundtrip happens.

        Raises:
            AlreadyRunning: Either the in-process flag is already set or
                the Postgres advisory lock is held by another worker.
        """
        self._status.mark_started(job_id)
        return await self.run_locked()

    #: Canonical stage names in the order :meth:`run_locked` executes them.
    #: Exposed so callers (endpoint / UI) can validate a stages filter
    #: without duplicating the list.
    ALL_STAGES: tuple[str, ...] = (
        "ingest", "embed", "classify", "hnsw", "tags", "combos", "deck_cards",
    )

    async def run_locked(self, stages: frozenset[str] | None = None) -> int:
        """Run one ingest cycle assuming status was already marked-started.

        §4.2 ordering, enforced here:

        1. Acquire ``pg_try_advisory_lock(ADVISORY_LOCK_KEY)``.
        2. Run the injected ingest body — populates ``cards_staging``.
        3. Execute :data:`COLLAPSE_SQL` to fold staging into ``cards``
           (DISTINCT ON oracle_id, oldest released_at wins).
        4. Execute :data:`TRUNCATE_STAGING_SQL` to leave staging empty.
        5. Release the lock, mark status finished — always, even if
           ingest fails.

        Collapse and truncate are skipped when ingest raises: collapsing
        a partial stage would corrupt ``cards`` with half-refreshed rows.

        The unlock step (D10.9) calls ``await conn.rollback()`` first so
        a preceding failure that left the SA connection in aborted-txn
        state cannot block the unlock-txn ``begin()`` — the advisory lock
        is released independently of any earlier transaction's outcome.

        Args:
            stages: Optional subset of :attr:`ALL_STAGES` to execute.
                ``None`` runs every stage (the default, full pipeline).
                Skipped stages are not recorded in ``/health`` telemetry.
                Ordering is fixed — a subset preserves the original order,
                it does not let callers reorder work. Caller-side gotcha:
                selecting ``ingest`` without ``hnsw`` leaves the staging
                table populated (``hnsw`` is the stage that truncates
                staging); next ``ingest`` run will double-insert rows,
                which the DISTINCT-ON collapse still deduplicates but
                wastes disk. Selecting ``hnsw`` alone is safe (truncate
                is idempotent on an empty staging table).

        Returns:
            The row count returned by the ingest function, or ``0`` when
            ``ingest`` was not in the selected stages.

        Raises:
            AlreadyRunning: The Postgres advisory lock is held by another
                worker (different process / replica).
        """
        def _want(name: str) -> bool:
            return stages is None or name in stages

        self._status.set_planned(tuple(n for n in self.ALL_STAGES if _want(n)))

        # Bug.6: gated by AlreadyRunning — losing replica must not bump
        # the search-response ETag epoch on the way out.
        already_running = False

        async def _run_ingest() -> int:
            if not _want("ingest"):
                return 0
            with self._status.stage("ingest") as st:
                # A failed run leaves staging populated (truncate is the
                # last step); start clean so collapse never sees a stale copy.
                async with self._engine.begin() as c:
                    await c.execute(text(TRUNCATE_STAGING_SQL))
                staged_local = await self._ingest(self._engine)
                st.rows = staged_local
                # COLLAPSE on its own pool conn so parallel branches don't
                # fight the lock-holding outer conn.
                async with self._engine.begin() as c:
                    await c.execute(text(COLLAPSE_SQL))
                    await c.execute(text(PRINTINGS_SQL))
            return staged_local

        async def _run_embed_hnsw() -> None:
            # Serial chain: embed feeds hnsw. hnsw also truncates staging.
            if _want("embed"):
                with self._status.stage("embed") as st:
                    st.rows = await self._embed(self._engine)
            if _want("hnsw"):
                with self._status.stage("hnsw"):
                    await self._rebuild_hnsw_concurrent()
                    async with self._engine.begin() as c:
                        await c.execute(text(HNSW_SWAP_SQL))
                        await c.execute(text(TRUNCATE_STAGING_SQL))

        async def _run_best_effort(name: str, body: Callable[[], Awaitable[int]]) -> None:
            if not _want(name):
                return
            try:
                with self._status.stage(name) as st:
                    st.rows = await body()
            except Exception:  # noqa: BLE001
                _log.exception("%s failed; cards refresh kept", name)

        try:
            # Session-level advisory lock held across the whole flow. The
            # outer conn is only for the lock — every stage opens its own
            # pool conn so the DAG's parallel branches don't contend.
            async with self._engine.connect() as conn:
                async with conn.begin():
                    lock_result = await conn.execute(
                        text(f"SELECT pg_try_advisory_lock({ADVISORY_LOCK_KEY})"),
                    )
                    granted = bool(lock_result.scalar_one())
                if not granted:
                    already_running = True
                    raise AlreadyRunning(
                        f"advisory lock {ADVISORY_LOCK_KEY} already held"
                    )

                try:
                    # DAG-driven parallelism. `tags` is fully independent
                    # of `ingest` so we kick it off up front and let it
                    # overlap with the ingest+collapse stage. Everything
                    # else (embed→hnsw, classify, combos) waits for ingest
                    # then fans out concurrently — see README "Refresh
                    # stage dependencies".
                    tags_task = asyncio.create_task(
                        _run_best_effort("tags", lambda: self._tags(self._engine)),
                        name="refresh-tags",
                    )
                    try:
                        staged = await _run_ingest()
                    except BaseException:
                        # ingest failed — cancel the piggy-backed tags task
                        # before propagating so we don't strand a coroutine
                        # past the advisory-unlock finally.
                        tags_task.cancel()
                        with suppress(BaseException):
                            await tags_task
                        raise

                    await asyncio.gather(
                        _run_embed_hnsw(),
                        _run_best_effort(
                            "classify", lambda: self._classify(self._engine)
                        ),
                        _run_best_effort(
                            "combos", lambda: self._combos(self._engine)
                        ),
                        # D19.24: depends on `ingest` (resolution reads
                        # `cards`), so it fans out here rather than beside
                        # the independent `tags`.
                        _run_best_effort(
                            "deck_cards", lambda: self._deck_cards(self._engine)
                        ),
                        tags_task,
                    )
                    return staged
                finally:
                    # D10.9: clear any aborted-txn state on `conn` BEFORE
                    # opening the unlock txn. If an inner step raised mid-
                    # transaction, the SA connection is in aborted state
                    # and the next implicit auto-begin (i.e. `conn.begin()`)
                    # would itself raise InvalidRequestError — stranding
                    # the session-scoped advisory lock until pool recycle.
                    # `rollback()` resets that state and is a no-op on a
                    # clean connection. Swallowed because rollback is
                    # best-effort: a still-broken conn surfaces via the
                    # begin() below, and we must not mask the in-flight
                    # exception that brought us into the finally.
                    with suppress(Exception):
                        await conn.rollback()
                    async with conn.begin():
                        await conn.execute(
                            text(f"SELECT pg_advisory_unlock({ADVISORY_LOCK_KEY})"),
                        )
        finally:
            self._status.mark_finished(bump_epoch=not already_running)

    async def _rebuild_hnsw_concurrent(self) -> None:
        """Rebuild `cards_embedding_hnsw` under a scratch name, CONCURRENTLY.

        Opens a separate connection with ``isolation_level="AUTOCOMMIT"``
        because ``CREATE INDEX CONCURRENTLY`` refuses to run inside a
        transaction. The lock-holding outer connection is left alone —
        the advisory lock still gates the caller against a competing
        refresh, and this scratch connection only touches the scratch
        index.

        Pre-cleanup drops any leftover `cards_embedding_hnsw_new` from a
        prior crashed refresh (would-be `INVALID` per Postgres semantics
        after a failed CONCURRENTLY). The subsequent
        ``CREATE INDEX CONCURRENTLY IF NOT EXISTS`` is then idempotent
        against both a clean slate and a partial-prior-run state.
        """
        async with self._engine.connect() as build_conn:
            autocommit = await build_conn.execution_options(
                isolation_level="AUTOCOMMIT"
            )
            await autocommit.execute(text(HNSW_CONCURRENT_CLEANUP_SQL))
            await autocommit.execute(text(HNSW_CONCURRENT_BUILD_SQL))
