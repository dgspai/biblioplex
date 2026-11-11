"""Stored-deck CRUD (D19.4).

The rest of `/decks/*` is stateless — a decklist string goes in the body
and nothing is kept. These routes are the library: decks persist, and
every write appends a `deck_versions` row so the history in D19.5 is
complete rather than only covering imports.

Lives in its own module rather than growing `routers/decks.py`, which is
already 79 KB and slated for a split (D16.37). Paths do not collide: the
stateless surface is all literal sub-paths (`/decks/analyze`, …) and this
router adds `/decks`, `/decks/upload` and `/decks/{deck_id}`.
"""

from __future__ import annotations

import uuid as _uuid
from pathlib import PurePosixPath
from typing import Any, Protocol
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi import Path as _FastPath
from fastapi.responses import Response
from pydantic import BaseModel, Field

from biblioplex.decklist.evaluate import evaluate_deck
from biblioplex.decklist.importers import to_decklist
from biblioplex.decklist.sections import parse_sections
from biblioplex.decks import service as deck_service
from biblioplex.errors import ErrorEnvelope
from biblioplex.errors import scryfall_error as _scryfall_error
from biblioplex.routers.cards import get_read_engine
from biblioplex.routers.decks import DECKLIST_MAX_LEN
from biblioplex.routers.decks._common import _analyze_pipeline


class _Engine(Protocol):
    """Read-pool engine. ``begin()`` is the write path — SQLAlchemy's
    ``connect()`` opens an implicit transaction that is ROLLED BACK on
    close, so every INSERT/UPDATE/DELETE here must go through it."""

    def connect(self) -> Any: ...

    def begin(self) -> Any: ...


router = APIRouter(prefix="/decks", tags=["decks"])

_ERROR_RESPONSE: dict[str, Any] = {
    "model": ErrorEnvelope,
    "description": "Scryfall-shaped error envelope.",
}

# One fixed service per column. A Moxfield URL pasted into `archidekt_url`
# is not a cosmetic mistake — it is what D14.18 would later try to fetch.
_URL_HOSTS = {
    "deckbox_url": "deckbox.org",
    "moxfield_url": "moxfield.com",
    "archidekt_url": "archidekt.com",
}


def _url_error(values: dict[str, Any]) -> str | None:
    """Return a message when a service URL points at the wrong host."""
    for field, host in _URL_HOSTS.items():
        raw = values.get(field)
        if not raw or not str(raw).strip():
            continue
        candidate = str(raw).strip()
        # People paste `deckbox.org/sets/123`; urlparse needs a scheme
        # before it will populate `.hostname`.
        if "//" not in candidate:
            candidate = f"https://{candidate}"
        hostname = (urlparse(candidate).hostname or "").lower()
        if hostname != host and not hostname.endswith(f".{host}"):
            return f"{field} must point at {host}"
    return None


def _decklist_error(decklist: str) -> str | None:
    if not decklist or not decklist.strip():
        return "decklist is required"
    if len(decklist) > DECKLIST_MAX_LEN:
        return "decklist too long"
    return None


def _branch_error(branch: str) -> str | None:
    """Mirrors `deck_versions.branch`'s CHECK (D19.33) at the boundary, so a
    typo is a 422 rather than a raw DB constraint error."""
    if branch not in deck_service.VALID_BRANCHES:
        return f"branch must be one of {', '.join(deck_service.VALID_BRANCHES)}"
    return None


def _trunk_branch_error(trunk_branch: str | None) -> str | None:
    """D19.38: `trunk_branch` must be a real branch or `None` — `None` is
    the "follow every import" spelling and must stay reachable, so this
    validates only when a caller actually sent a value."""
    if trunk_branch is not None and trunk_branch not in deck_service.VALID_BRANCHES:
        return f"trunk_branch must be one of {', '.join(deck_service.VALID_BRANCHES)}"
    return None


def _source_error(source: str) -> str | None:
    """Mirrors `deck_versions.source`'s CHECK (D19.1) at the boundary."""
    if source not in deck_service.VALID_SOURCES:
        return f"source must be one of {', '.join(deck_service.VALID_SOURCES)}"
    return None


def _derive_name(explicit: str | None, decklist: str, filename: str | None) -> str:
    """Explicit name, else the export's own, else the filename, else a placeholder.

    Arena writes `Name <deck>` in its `About` block and D19.2 already
    recovers it — asking the user to retype what the file states would be
    gratuitous.
    """
    if explicit and explicit.strip():
        return explicit.strip()[: deck_service.MAX_NAME_LEN]
    parsed = parse_sections(decklist).name
    if parsed:
        return parsed[: deck_service.MAX_NAME_LEN]
    if filename:
        stem = PurePosixPath(filename).stem.strip()
        if stem:
            return stem[: deck_service.MAX_NAME_LEN]
    return deck_service.DEFAULT_DECK_NAME


def _is_uuid(value: str) -> bool:
    try:
        _uuid.UUID(value)
    except ValueError:
        return False
    return True


class CreateDeckRequest(BaseModel):
    """Body for ``POST /decks``."""

    decklist: str = Field(
        ...,
        description=(
            "Deck content. Plaintext decklist, or a Deckbox CSV / print-view "
            "HTML export pasted verbatim — the format is sniffed from the "
            "content and coerced."
        ),
        examples=["Commander\n1 Hope of Ghirapur\n\nDeck\n1 Sol Ring\n"],
    )
    folder: str | None = Field(
        default=None,
        description=(
            "Folder path, `/`-separated for nesting (`Commander/Budget`). "
            "Null or empty leaves the deck unfiled. Normalised on write: "
            "segments trimmed, repeated and edge separators collapsed. Case "
            "is preserved, so `Commander` and `commander` are two folders."
        ),
        examples=["Commander/Budget"],
    )
    name: str | None = Field(
        default=None,
        description=(
            "Deck name. Defaults to the name carried by the export (Arena's "
            "`Name` line), then to `Untitled deck`."
        ),
        examples=["Iroh gift"],
    )
    format: str = Field(
        default="commander",
        description="Format slug, matching Scryfall's `legalities` keys.",
        examples=["commander"],
    )
    commander: str | None = Field(
        default=None,
        description=(
            "Commander override. Leave null to derive it from the decklist's "
            "`Commander` section; set it for exports that carry no such "
            "section, which is every Deckbox one."
        ),
        examples=["Iroh, Grand Lotus"],
    )
    companion: str | None = Field(
        default=None,
        description=(
            "Companion override. Leave null to derive it from the decklist's "
            "`Companion` section; set it for exports that carry no such "
            "section, which is every format seen so far."
        ),
        examples=["Yorion, Sky Nomad"],
    )
    notes: str | None = Field(
        default=None, description="Free-text notes.", examples=["gift deck"]
    )
    deckbox_url: str | None = Field(
        default=None,
        description="Link to the same deck on deckbox.org.",
        examples=["https://deckbox.org/sets/3293466"],
    )
    moxfield_url: str | None = Field(
        default=None,
        description="Link to the same deck on moxfield.com.",
        examples=["https://moxfield.com/decks/abc123"],
    )
    archidekt_url: str | None = Field(
        default=None,
        description="Link to the same deck on archidekt.com.",
        examples=["https://archidekt.com/decks/123456"],
    )
    branch: str = Field(
        default="main",
        description=(
            "Which version-history branch this write lands on — `main`, "
            "`deckbox`, `moxfield`, or `archidekt`. Dedup and the diff base "
            "used to compare imports are scoped per branch, so importing an "
            "unchanged Moxfield list right after a Deckbox one is not seen "
            "as a change."
        ),
        examples=["main"],
    )
    message: str | None = Field(
        default=None,
        description="Optional commit-style note describing this version.",
        examples=["initial import"],
    )


class UpdateDeckRequest(BaseModel):
    """Body for ``PATCH /decks/{deck_id}``. Every field is optional."""

    decklist: str | None = Field(
        default=None,
        description=(
            "Replacement deck content. Appends a version unless identical "
            "to the current one."
        ),
        examples=["1 Sol Ring\n1 Mana Crypt\n"],
    )
    folder: str | None = Field(
        default=None,
        description=(
            "Folder path, `/`-separated for nesting (`Commander/Budget`). "
            "Null or empty leaves the deck unfiled. Normalised on write: "
            "segments trimmed, repeated and edge separators collapsed. Case "
            "is preserved, so `Commander` and `commander` are two folders."
        ),
        examples=["Commander/Budget"],
    )
    name: str | None = Field(
        default=None, description="Deck name.", examples=["Iroh gift v2"]
    )
    format: str | None = Field(
        default=None, description="Format slug.", examples=["commander"]
    )
    commander: str | None = Field(
        default=None, description="Commander override.", examples=["Iroh, Grand Lotus"]
    )
    companion: str | None = Field(
        default=None, description="Companion override.", examples=["Yorion, Sky Nomad"]
    )
    notes: str | None = Field(
        default=None, description="Free-text notes.", examples=["swapped ramp"]
    )
    deckbox_url: str | None = Field(
        default=None, description="deckbox.org link.", examples=["https://deckbox.org/sets/3293466"]
    )
    moxfield_url: str | None = Field(
        default=None, description="moxfield.com link.", examples=["https://moxfield.com/decks/abc123"]
    )
    archidekt_url: str | None = Field(
        default=None, description="archidekt.com link.", examples=["https://archidekt.com/decks/123456"]
    )
    trunk_branch: str | None = Field(
        default=None,
        description=(
            "The one branch allowed to move head. `null` (default) means "
            "follow every import — today's behaviour. Set to `main`, "
            "`deckbox`, `moxfield`, or `archidekt` to stop head flipping "
            "with sync order; imports onto any OTHER branch still append "
            "a version but leave head, `coverage_pct`, and the card table "
            "untouched."
        ),
        examples=["main"],
    )
    branch: str = Field(
        default="main",
        description=(
            "Branch this `decklist` write lands on when supplied. Ignored "
            "when `decklist` is omitted (a metadata-only PATCH touches no "
            "version)."
        ),
        examples=["deckbox"],
    )
    message: str | None = Field(
        default=None,
        description="Optional commit-style note for the new version.",
        examples=["swapped ramp"],
    )



def _folder_or_error(raw: str | None) -> tuple[str | None, str | None]:
    """Normalise a folder path. Returns ``(value, error_message)``.

    Validation lives at the boundary so a bad path is a 422 rather than a
    row carrying a newline that no tree UI can render.
    """
    try:
        return deck_service.normalize_folder(raw), None
    except ValueError as exc:
        return None, str(exc)


@router.post(
    "",
    summary="Store a new deck from pasted text",
    description=(
        "Creates a deck and its version 1. `decklist` may be a plaintext "
        "decklist or a pasted Deckbox CSV / print-view HTML export — the "
        "format is sniffed from the content, never from a filename. Section "
        "headings (`Commander`, `Deck`, `Sideboard`) are understood, so the "
        "commander is derived automatically when the export carries one.\n\n"
        "Deck content is versioned: this endpoint writes version 1, and every "
        "later change appends another. Errors: 422 `bad_request` for an empty "
        "or over-long decklist (8000-char cap) or a service URL pointing at "
        "the wrong host. Auto-registered as MCP tool `create_deck`."
    ),
    responses={422: _ERROR_RESPONSE},
)
async def create_deck(
    req: CreateDeckRequest,
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Store a new deck from pasted decklist, CSV, or HTML text."""
    decklist = to_decklist(req.decklist)
    err = _decklist_error(decklist)
    if err:
        return _scryfall_error(422, "bad_request", err)
    url_err = _url_error(req.model_dump())
    if url_err:
        return _scryfall_error(422, "bad_request", url_err)
    folder, folder_err = _folder_or_error(req.folder)
    if folder_err:
        return _scryfall_error(422, "bad_request", folder_err)
    branch_err = _branch_error(req.branch)
    if branch_err:
        return _scryfall_error(422, "bad_request", branch_err)

    async with engine.begin() as conn:
        return await deck_service.create_deck(
            conn,
            name=_derive_name(req.name, decklist, None),
            decklist=decklist,
            source="paste",
            branch=req.branch,
            message=req.message,
            deck_format=req.format,
            commander=req.commander,
            companion=req.companion,
            notes=req.notes,
            deckbox_url=req.deckbox_url,
            moxfield_url=req.moxfield_url,
            archidekt_url=req.archidekt_url,
            folder=folder,
            raw_content=None,
        )


@router.post(
    "/upload",
    summary="Store a new deck from an uploaded export file",
    description=(
        "Multipart upload of a deck export (`file=@deck.txt`). Accepts every "
        "shape in `data/example_deck_exports/`: Moxfield plain / Arena / MTGO / "
        "set-code variants, Archidekt, and Deckbox txt, CSV and print-view "
        "HTML. Format is sniffed from the file's content, not its extension.\n\n"
        "The original bytes are retained on the version so a later download "
        "returns the exact file that was imported. Errors: 422 `bad_request` "
        "for an empty or over-long decklist or a bad service URL. "
        "Auto-registered as MCP tool `upload_deck`."
    ),
    responses={422: _ERROR_RESPONSE},
)
async def upload_deck(
    file: UploadFile = File(description="Deck export file (.txt, .csv, or .html)"),
    name: str | None = Form(
        default=None, description="Deck name; defaults to the filename stem"
    ),
    format: str = Form(default="commander", description="Format slug"),
    commander: str | None = Form(default=None, description="Commander override"),
    companion: str | None = Form(default=None, description="Companion override"),
    notes: str | None = Form(default=None, description="Free-text notes"),
    deckbox_url: str | None = Form(default=None, description="deckbox.org link"),
    moxfield_url: str | None = Form(default=None, description="moxfield.com link"),
    archidekt_url: str | None = Form(default=None, description="archidekt.com link"),
    branch: str = Form(
        default="main",
        description="Branch this import lands on (`main`/`deckbox`/`moxfield`/`archidekt`)",
    ),
    message: str | None = Form(default=None, description="Optional commit-style note"),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Store a new deck from an uploaded export file."""
    raw = await file.read()
    # `errors="replace"` keeps one stray byte from 500-ing a whole import,
    # matching the D18.7 inventory upload path.
    decklist = to_decklist(raw.decode("utf-8", errors="replace"))
    err = _decklist_error(decklist)
    if err:
        return _scryfall_error(422, "bad_request", err)
    urls = {
        "deckbox_url": deckbox_url,
        "moxfield_url": moxfield_url,
        "archidekt_url": archidekt_url,
    }
    url_err = _url_error(urls)
    if url_err:
        return _scryfall_error(422, "bad_request", url_err)
    branch_err = _branch_error(branch)
    if branch_err:
        return _scryfall_error(422, "bad_request", branch_err)

    async with engine.begin() as conn:
        return await deck_service.create_deck(
            conn,
            name=_derive_name(name, decklist, file.filename),
            decklist=decklist,
            source="upload",
            branch=branch,
            message=message,
            deck_format=format,
            commander=commander,
            companion=companion,
            notes=notes,
            filename=file.filename,
            raw_content=raw,
            **urls,
        )


@router.get(
    "",
    summary="List stored decks",
    description=(
        "Newest-touched-first listing of the deck library, with an optional "
        "case-insensitive name filter. Each row carries the current version "
        "number and its card counts, plus the effective commander — but NOT "
        "the decklist body, which would make browsing ship every deck in "
        "full. Fetch one deck to get its content. "
        "Auto-registered as MCP tool `list_decks`."
    ),
)
async def list_decks(
    q: str | None = Query(
        default=None, description="Case-insensitive deck-name filter", examples=["iroh"]
    ),
    page: int = Query(default=1, ge=1, description="Page number (1-indexed)", examples=[1]),
    limit: int = Query(default=50, ge=1, le=200, description="Decks per page", examples=[50]),
    folder: str | None = Query(
        default=None,
        description=(
            "Filter to one folder path. Pass it EMPTY (`?folder=`) to list "
            "only unfiled decks — that is a different request from omitting "
            "the parameter, which applies no folder filter at all."
        ),
        examples=["Commander/Budget"],
    ),
    include_subfolders: bool = Query(
        default=False,
        description="Widen `folder` to the whole subtree beneath it.",
        examples=[False],
    ),
    sort: str = Query(
        default="updated",
        description=(
            "`updated` (newest-touched first, the default), `name`, `coverage` "
            "— how much of each deck you already own, as a ratio so a "
            "fully-built small deck outranks a larger unfinished one — or "
            "`missing_cost`, the rough price of the cards it still needs "
            "(cheapest printing per card; ascends by default so the deck "
            "you can finish most cheaply leads)."
        ),
        examples=["coverage"],
    ),
    direction: str | None = Query(
        default=None,
        description=(
            "`asc` or `desc`. Defaults per key: coverage and updated "
            "descending, name ascending. Ascending coverage answers "
            "\"what do I still need to buy?\"."
        ),
        examples=["desc"],
    ),
    completion: str | None = Query(
        default=None,
        description=(
            "Filter by whether you already own every card: `complete` "
            "(nothing missing) or `incomplete` (at least one card short). "
            "Ownership is the same figure `coverage_pct` reports, so the "
            "filter and the number on each row cannot disagree. A deck the "
            "derived index has not covered yet matches NEITHER value — "
            "\"not indexed\" is not \"you own all of it\"."
        ),
        examples=["incomplete"],
    ),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """List stored decks with their current version summary."""
    # `?folder=` (present but empty) means unfiled; omitted means no filter.
    unfiled = folder is not None and not folder.strip()
    value, err = _folder_or_error(None if unfiled else folder)
    if err:
        return _scryfall_error(422, "bad_request", err)

    async with engine.connect() as conn:
        try:
            return await deck_service.list_decks(
                conn,
                q=q,
                page=page,
                limit=limit,
                folder=value,
                include_subfolders=include_subfolders,
                unfiled=unfiled,
                sort=sort,
                direction=direction,
                completion=completion,
            )
        except ValueError as exc:
            return _scryfall_error(422, "bad_request", str(exc))


@router.get(
    "/completion-plan",
    summary="What to buy next to finish whole decks",
    description=(
        "Two greedy shopping plans over your incomplete decks, optimising "
        "DECKS COMPLETED rather than cards-per-dollar — a card five decks "
        "want that finishes none of them buys nothing playable.\n\n"
        "`by_cost` minimises spend; `by_cards` minimises how many cards you "
        "buy. Neither is the default and both are returned, because they "
        "diverge sharply: on a real library at `budget=10` both complete 3 "
        "decks while buying 20 cards vs 5.\n\n"
        "Costs are MARGINAL — each step is priced against what earlier steps "
        "already bought, and one bought copy satisfies every deck that wants "
        "one, so a duplicated deck can complete for $0.00. Summing per-deck "
        "`missing_usd` instead overstates.\n\n"
        "A deck whose gap includes an unpriced card cannot be ranked by cost: "
        "`by_cost` reports it under `skipped_unpriced`, while `by_cards` can "
        "rank it (card counts need no prices) and marks its total a floor via "
        "`spend_usd_is_floor`. Greedy is not proven optimal and the response "
        "says so.\n\n"
        "Both `budget` and `max_cards` are optional; with neither, the plan "
        "orders every completable deck. Auto-registered as MCP tool "
        "`get_deck_completion_plan`."
    ),
)
async def get_deck_completion_plan(
    budget: float | None = Query(
        default=None,
        ge=0,
        description="Spend ceiling in USD. Omit to plan every completable deck.",
        examples=[10],
    ),
    max_cards: int | None = Query(
        default=None,
        ge=1,
        description="Cap on how many cards to buy. Composes with `budget`.",
        examples=[6],
    ),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Two greedy plans for finishing decks within a budget."""
    async with engine.connect() as conn:
        return await deck_service.completion_plan(
            conn, budget=budget, max_cards=max_cards
        )


@router.get(
    "/folders",
    summary="List deck folders with their counts",
    description=(
        "Every folder path in the library, with the number of decks filed "
        "directly in it (`deck_count`) and including its descendants "
        "(`total_deck_count`), plus `unfiled_count` for decks in no folder. "
        "Ancestors are DERIVED: a deck filed at `Commander/Budget` yields a "
        "`Commander` node even when no deck sits directly in it, so a caller "
        "can build a tree without inferring missing parents. "
        "Auto-registered as MCP tool `list_deck_folders`."
    ),
)
async def list_deck_folders(
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """List every folder path with its direct and subtree deck counts."""
    async with engine.connect() as conn:
        return await deck_service.list_folders(conn)



class RenameFolderRequest(BaseModel):
    """Body for ``POST /decks/folders/rename``."""

    from_: str = Field(
        ...,
        alias="from",
        description=(
            "Folder path to move. Matches that folder and everything beneath "
            "it; a sibling merely sharing a name prefix (`CommanderX` for "
            "`Commander`) is never touched."
        ),
        examples=["Commander"],
    )
    to: str | None = Field(
        default=None,
        description=(
            "Destination path. Null or empty UNFILES the subtree, which is "
            "also how a folder is deleted — there is no folder row to remove."
        ),
        examples=["Legacy/Commander"],
    )


@router.post(
    "/folders/rename",
    summary="Rename or move a folder subtree",
    description=(
        "Re-files a folder and every deck beneath it in one statement, and "
        "the repair path for the phantom folders a typo or a case variant "
        "creates. Descendants keep their relative path: renaming "
        "`Commander` to `Legacy` moves `Commander/Budget` to "
        "`Legacy/Budget`. Omitting `to` unfiles the whole subtree. "
        "Renaming an unknown folder is not an error — it reports "
        "`decks_moved: 0`. "
        "Errors: 422 `bad_request` for a missing source or an invalid path. "
        "Auto-registered as MCP tool `rename_deck_folder`."
    ),
    responses={422: _ERROR_RESPONSE},
)
async def rename_deck_folder(
    req: RenameFolderRequest,
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Move a folder and all decks beneath it to a new path."""
    try:
        async with engine.begin() as conn:
            moved = await deck_service.rename_folder(conn, req.from_, req.to)
    except ValueError as exc:
        return _scryfall_error(422, "bad_request", str(exc))

    # Safe to re-normalise unguarded: `rename_folder` already validated
    # both sides above, so an invalid path has 422'd before reaching here.
    return {
        "object": "folder_renamed",
        "from": deck_service.normalize_folder(req.from_),
        "to": deck_service.normalize_folder(req.to),
        "decks_moved": moved,
    }


@router.get(
    "/{deck_id}",
    summary="Fetch one stored deck with its current decklist",
    description=(
        "Returns the deck's metadata plus the decklist text of its current "
        "(highest-numbered) version. The `decklist` field round-trips: POST it "
        "straight back to `/decks/analyze`, `/decks/legality` or any other "
        "stateless endpoint unchanged.\n\n"
        "`commander` is the effective value — the explicit override when set, "
        "otherwise whatever the decklist's `Commander` section names, with "
        "partners joined by ` // `. Errors: 404 `not_found`. "
        "Auto-registered as MCP tool `get_deck`."
    ),
    responses={404: _ERROR_RESPONSE},
)
async def get_deck(
    deck_id: str = _FastPath(
        description="Deck id (uuid)", examples=["11111111-1111-1111-1111-111111111111"]
    ),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Fetch one stored deck and its current decklist."""
    if not _is_uuid(deck_id):
        return _scryfall_error(404, "not_found", f"No deck with id {deck_id}")
    async with engine.connect() as conn:
        deck = await deck_service.get_deck(conn, deck_id)
    if deck is None:
        return _scryfall_error(404, "not_found", f"No deck with id {deck_id}")
    return deck


@router.get(
    "/{deck_id}/evaluate",
    summary="Evaluate a stored deck against the EDH template",
    description=(
        "Grades the deck's HEAD decklist against the guide template "
        "(`docs/edh-guide.md`) and returns a `summary` plus an ordered "
        "list of improvement `tips`. Each tip is `{code, severity, "
        "message}` — `severity` is `warn` for a hard template gap (lands "
        "below 38, a functional slot under target) or `info` for curve "
        "shaping, plan headroom and ownership. The full `/decks/analyze` "
        "payload rides along under `analysis` so a client renders the "
        "curve and slot bars without a second call. Errors: 404 "
        "`not_found`. Auto-registered as MCP tool `evaluate_deck`."
    ),
    responses={404: _ERROR_RESPONSE},
)
async def evaluate_stored_deck(
    deck_id: str = _FastPath(
        description="Deck id (uuid)",
        examples=["11111111-1111-1111-1111-111111111111"],
    ),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Evaluate a stored deck's HEAD version against the template."""
    if not _is_uuid(deck_id):
        return _scryfall_error(404, "not_found", f"No deck with id {deck_id}")
    async with engine.connect() as conn:
        deck = await deck_service.get_deck(conn, deck_id)
        if deck is None:
            return _scryfall_error(404, "not_found", f"No deck with id {deck_id}")
        analysis = await _analyze_pipeline(deck["decklist"], conn)
    result = evaluate_deck(analysis)
    return {
        "object": "deck_evaluation",
        "deck_id": deck_id,
        "name": deck.get("name"),
        "summary": result["summary"],
        "tips": result["tips"],
        "analysis": analysis,
    }


@router.patch(
    "/{deck_id}",
    summary="Update a stored deck's metadata and/or decklist",
    description=(
        "Patches any subset of the deck's fields. Supplying `decklist` appends "
        "a new version with `source = 'edit'` — UNLESS the text is identical "
        "to the current version, which is a no-op that returns the existing "
        "one so re-saving does not junk up the history. The response's "
        "`version_created` says which happened.\n\n"
        "Errors: 404 `not_found`; 422 `bad_request` for an over-long decklist "
        "or a service URL pointing at the wrong host. "
        "Auto-registered as MCP tool `update_deck`."
    ),
    responses={404: _ERROR_RESPONSE, 422: _ERROR_RESPONSE},
)
async def update_deck(
    req: UpdateDeckRequest,
    deck_id: str = _FastPath(
        description="Deck id (uuid)", examples=["11111111-1111-1111-1111-111111111111"]
    ),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Update a stored deck's metadata and/or append a new decklist version."""
    if not _is_uuid(deck_id):
        return _scryfall_error(404, "not_found", f"No deck with id {deck_id}")

    supplied = req.model_dump(exclude_unset=True)
    if "folder" in supplied:
        supplied["folder"], folder_err = _folder_or_error(supplied["folder"])
        if folder_err:
            return _scryfall_error(422, "bad_request", folder_err)
    if "trunk_branch" in supplied:
        trunk_err = _trunk_branch_error(supplied["trunk_branch"])
        if trunk_err:
            return _scryfall_error(422, "bad_request", trunk_err)
    decklist = supplied.pop("decklist", None)
    branch = supplied.pop("branch", "main")
    message = supplied.pop("message", None)
    if decklist is not None:
        decklist = to_decklist(decklist)
        err = _decklist_error(decklist)
        if err:
            return _scryfall_error(422, "bad_request", err)
        branch_err = _branch_error(branch)
        if branch_err:
            return _scryfall_error(422, "bad_request", branch_err)
    url_err = _url_error(supplied)
    if url_err:
        return _scryfall_error(422, "bad_request", url_err)

    async with engine.begin() as conn:
        deck = await deck_service.update_deck(
            conn,
            deck_id,
            fields=supplied,
            decklist=decklist,
            source="edit",
            branch=branch,
            message=message,
        )
    if deck is None:
        return _scryfall_error(404, "not_found", f"No deck with id {deck_id}")
    return deck


@router.delete(
    "/{deck_id}",
    summary="Delete a stored deck and its whole history",
    description=(
        "Removes the deck and every version it has ever had, via "
        "`ON DELETE CASCADE`. This is not recoverable — download any version "
        "worth keeping first. Errors: 404 `not_found`. "
        "Auto-registered as MCP tool `delete_deck`."
    ),
    responses={404: _ERROR_RESPONSE},
)
async def delete_deck(
    deck_id: str = _FastPath(
        description="Deck id (uuid)", examples=["11111111-1111-1111-1111-111111111111"]
    ),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Delete a stored deck and every version of it."""
    if not _is_uuid(deck_id):
        return _scryfall_error(404, "not_found", f"No deck with id {deck_id}")
    async with engine.begin() as conn:
        deleted = await deck_service.delete_deck(conn, deck_id)
    if not deleted:
        return _scryfall_error(404, "not_found", f"No deck with id {deck_id}")
    return {"object": "deck_deleted", "id": deck_id}


# ---- D19.5: version history ----


@router.post(
    "/{deck_id}/versions",
    summary="Import a new version onto an existing deck",
    description=(
        "Multipart upload of a deck export (`file=@deck.txt`) that appends "
        "onto an EXISTING deck, rather than creating a new one — the "
        "control the branch model was built for. The file goes through "
        "the identical `to_decklist` coercion `POST /decks/upload` uses, "
        "then through the same append machinery every write path shares: "
        "per-branch dedup, head auto-follow, the `head_pinned` guard and "
        "the `deck_cards` rebuild all apply unchanged. The original bytes "
        "are retained so a later download returns the exact file that was "
        "imported. Pasting text onto an existing deck is a separate, "
        "already-shipped path — `PATCH /decks/{deck_id}` with a "
        "`decklist` body — not duplicated here.\n\n"
        "Errors: 404 `not_found` for an unknown deck; 422 `bad_request` "
        "for an empty or over-long decklist, an unknown `branch`, or an "
        "unknown `source`. "
        "Auto-registered as MCP tool `add_deck_version`."
    ),
    responses={404: _ERROR_RESPONSE, 422: _ERROR_RESPONSE},
)
async def add_deck_version(
    deck_id: str = _FastPath(
        description="Deck id (uuid)", examples=["11111111-1111-1111-1111-111111111111"]
    ),
    file: UploadFile = File(description="Deck export file (.txt, .csv, or .html)"),
    branch: str = Form(
        default="main",
        description="Branch this import lands on (`main`/`deckbox`/`moxfield`/`archidekt`)",
        examples=["moxfield"],
    ),
    message: str | None = Form(
        default=None, description="Optional commit-style note", examples=["monthly resync"]
    ),
    source: str = Form(
        default="upload",
        description=(
            "Version source label. Defaults to `upload` since this route "
            "always carries a file; override only for a scripted caller "
            "reporting where the file actually came from."
        ),
        examples=["upload"],
    ),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Append a new version onto an existing deck from an uploaded file."""
    if not _is_uuid(deck_id):
        return _scryfall_error(404, "not_found", f"No deck with id {deck_id}")
    raw = await file.read()
    decklist = to_decklist(raw.decode("utf-8", errors="replace"))
    err = _decklist_error(decklist)
    if err:
        return _scryfall_error(422, "bad_request", err)
    branch_err = _branch_error(branch)
    if branch_err:
        return _scryfall_error(422, "bad_request", branch_err)
    source_err = _source_error(source)
    if source_err:
        return _scryfall_error(422, "bad_request", source_err)

    async with engine.begin() as conn:
        deck = await deck_service.update_deck(
            conn,
            deck_id,
            decklist=decklist,
            source=source,
            branch=branch,
            message=message,
            filename=file.filename,
            raw_content=raw,
        )
    if deck is None:
        return _scryfall_error(404, "not_found", f"No deck with id {deck_id}")
    return deck


def _content_disposition(filename: str) -> str:
    """Quote a download filename, stripping anything that could escape the header.

    The name comes from a user-supplied upload, so a CR/LF or a quote in it
    would otherwise let the caller inject arbitrary response headers.
    """
    safe = filename.replace("\r", " ").replace("\n", " ").replace('"', "'").strip()
    return f'attachment; filename="{safe or "deck.txt"}"'



@router.get(
    "/{deck_id}/cards",
    summary="List a deck's cards as table rows with ownership",
    description=(
        "The deck as a table rather than a text blob. One row per distinct "
        "card, carrying `section` (`commander` / `main` / `sideboard`), "
        "`qty`, the card `id` for linking to `/cards/:id`, type line, mana "
        "cost, cmc, price, and quantity-aware ownership: `qty_owned`, "
        "`qty_missing`, `on_wishlist`.\n\n"
        "Rows are flat with a `section` field, not grouped, so a client can "
        "sort the whole deck by ownership. Unlike `POST /decks/analyze` "
        "(which reads the mainboard only) the SIDEBOARD is included.\n\n"
        "Ownership sums across printings, so a card owned in two editions "
        "counts once at its combined quantity. Cards whose names resolve to "
        "nothing still appear, with `id: null` and `resolved: false`, so the "
        "table always agrees with the deck's own card count.\n\n"
        "`version` optionally requests a SPECIFIC version's table instead of "
        "HEAD's — computed live from that version's decklist rather than "
        "read from the derived index, since the index only ever reflects "
        "HEAD (D19.34). This never moves or touches the deck's head "
        "pointer; it is a read, matching `GET /decks/{id}/versions/{version}`. "
        "When `version` is given, the response also carries top-level "
        "`commander` / `companion` — the effective values for THAT "
        "version, not HEAD's, so a caller viewing another branch does not "
        "have to pair this with a second request to know who the "
        "commander is on the branch it just switched to (D19.39). "
        "Errors: 404 `not_found` for an unknown or malformed id, or an "
        "unknown `version`. Auto-registered as MCP tool `list_deck_cards`."
    ),
    responses={404: _ERROR_RESPONSE},
)
async def list_deck_cards(
    deck_id: str = _FastPath(
        description="Deck id (uuid)", examples=["11111111-1111-1111-1111-111111111111"]
    ),
    version: int | None = Query(
        default=None,
        description=(
            "Read this version's cards instead of HEAD's. Does not move the "
            "deck's head pointer — use POST /decks/{id}/head to promote."
        ),
        examples=[3],
    ),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """List one deck's cards with per-card inventory ownership."""
    if not _is_uuid(deck_id):
        return _scryfall_error(404, "not_found", f"No deck with id {deck_id}")
    async with engine.connect() as conn:
        out = await deck_service.list_deck_cards(conn, deck_id, version=version)
    if out is None:
        return _scryfall_error(404, "not_found", f"No deck with id {deck_id}")
    return out


@router.get(
    "/{deck_id}/branches",
    summary="Compare every synced branch's head against the deck's HEAD",
    description=(
        "Answers \"are my synced copies in sync?\" with NO fetch — every "
        "branch's own latest version is already stored locally "
        "(D19.33/D19.34), so this is a pure local comparison, not a pull "
        "from Deckbox/Moxfield/Archidekt. One row per branch that has AT "
        "LEAST ONE version: `branch`, `version` (that branch's own "
        "latest), `updated_at`, `is_head` (true for the branch/version "
        "the deck's head currently points at), and `differs` — `added` / "
        "`removed` / `changed` card counts against HEAD's content.\n\n"
        "Both sides fold through the same partition-aware helper the "
        "persisted card table uses (`deck_card_rows`) before comparing — "
        "never raw text. Two exports of an identical deck differ "
        "byte-for-byte (line order, comments, tail annotations like `4x` "
        "vs `4 `) and still report IN SYNC (`differs` all zero). Reuses "
        "the SAME classifier `POST /decks/diff` runs, so the counts here "
        "always agree with a manual diff of the same two versions. The "
        "head's own row is flagged rather than diffed against itself.\n\n"
        "Errors: 404 `not_found` for an unknown or malformed id. "
        "Auto-registered as MCP tool `list_deck_branches`."
    ),
    responses={404: _ERROR_RESPONSE},
)
async def list_deck_branches(
    deck_id: str = _FastPath(
        description="Deck id (uuid)", examples=["11111111-1111-1111-1111-111111111111"]
    ),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """List every branch's head, compared against the deck's current HEAD."""
    if not _is_uuid(deck_id):
        return _scryfall_error(404, "not_found", f"No deck with id {deck_id}")
    async with engine.connect() as conn:
        out = await deck_service.list_branches(conn, deck_id)
    if out is None:
        return _scryfall_error(404, "not_found", f"No deck with id {deck_id}")
    return out


@router.get(
    "/{deck_id}/versions",
    summary="List a deck's version history",
    description=(
        "Newest-first history of every state this deck's decklist has been "
        "in — imports AND manual edits, so a diff between any two points is "
        "complete. Each row carries its `source` (`upload` / `paste` / `url` / "
        "`edit` / `restore`), the originating filename when there was one, and "
        "the card counts that version produced.\n\n"
        "Bodies are excluded: fetch one version for its text, or "
        "`/download` for the original file. To diff two versions, fetch both "
        "and POST them to `/decks/diff` — there is no separate diff endpoint. "
        "Errors: 404 `not_found`. Auto-registered as MCP tool "
        "`list_deck_versions`."
    ),
    responses={404: _ERROR_RESPONSE},
)
async def list_deck_versions(
    deck_id: str = _FastPath(
        description="Deck id (uuid)", examples=["11111111-1111-1111-1111-111111111111"]
    ),
    limit: int = Query(default=100, ge=1, le=500, description="Max versions", examples=[100]),
    branch: str | None = Query(
        default=None,
        description="Filter to one branch's own history (`main`/`deckbox`/`moxfield`/`archidekt`).",
        examples=["deckbox"],
    ),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """List every stored version of one deck, newest first."""
    if not _is_uuid(deck_id):
        return _scryfall_error(404, "not_found", f"No deck with id {deck_id}")
    if branch is not None:
        branch_err = _branch_error(branch)
        if branch_err:
            return _scryfall_error(422, "bad_request", branch_err)
    async with engine.connect() as conn:
        versions = await deck_service.list_versions(conn, deck_id, limit=limit, branch=branch)
        if not versions:
            # A deck always has at least version 1, so with NO branch filter
            # "no versions" means no deck — saving an existence query on the
            # hot path. A branch filter can legitimately come back empty for
            # a real deck that has never been touched on that branch, so
            # that shortcut cannot apply here.
            if branch is not None:
                if not await deck_service.deck_exists(conn, deck_id):
                    return _scryfall_error(404, "not_found", f"No deck with id {deck_id}")
                return {"object": "list", "deck_id": deck_id, "data": []}
            return _scryfall_error(404, "not_found", f"No deck with id {deck_id}")
    return {"object": "list", "deck_id": deck_id, "data": versions}


@router.get(
    "/{deck_id}/versions/{version}",
    summary="Fetch one past version's decklist",
    description=(
        "Returns the canonical decklist text of a single version. The text "
        "round-trips — POST it to `/decks/analyze`, or to `/decks/diff` "
        "alongside another version to see exactly what changed between them. "
        "Errors: 404 `not_found`. Auto-registered as MCP tool "
        "`get_deck_version`."
    ),
    responses={404: _ERROR_RESPONSE},
)
async def get_deck_version(
    deck_id: str = _FastPath(
        description="Deck id (uuid)", examples=["11111111-1111-1111-1111-111111111111"]
    ),
    version: int = _FastPath(description="1-based version number", examples=[2]),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Fetch one past version of a deck, including its decklist text."""
    if not _is_uuid(deck_id):
        return _scryfall_error(404, "not_found", f"No deck with id {deck_id}")
    async with engine.connect() as conn:
        row = await deck_service.get_version(conn, deck_id, version)
    if row is None:
        return _scryfall_error(404, "not_found", f"No version {version} for deck {deck_id}")
    return {"object": "deck_version", "deck_id": deck_id, **row}


@router.get(
    "/{deck_id}/versions/{version}/download",
    summary="Download a past version as a file",
    description=(
        "Streams a version back for saving. When that version came from an "
        "uploaded file, the ORIGINAL bytes are returned verbatim under their "
        "original filename — the exact CSV or HTML that was imported, not a "
        "re-serialisation of it. Versions created by paste, edit or restore "
        "have no source file, so the canonical decklist text is returned as "
        "`<name>-v<N>.txt` instead.\n\n"
        "Errors: 404 `not_found`. Auto-registered as MCP tool "
        "`download_deck_version`."
    ),
    response_class=Response,
    responses={404: _ERROR_RESPONSE},
)
async def download_deck_version(
    deck_id: str = _FastPath(
        description="Deck id (uuid)", examples=["11111111-1111-1111-1111-111111111111"]
    ),
    version: int = _FastPath(description="1-based version number", examples=[2]),
    name: str = Query(
        default="deck", description="Base filename for the text fallback", examples=["Iroh gift"]
    ),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Download one version as its original file, or as canonical text."""
    if not _is_uuid(deck_id):
        return _scryfall_error(404, "not_found", f"No deck with id {deck_id}")
    async with engine.connect() as conn:
        blob = await deck_service.get_version_blob(conn, deck_id, version)
    if blob is None:
        return _scryfall_error(404, "not_found", f"No version {version} for deck {deck_id}")

    if blob["raw_content"] is not None:
        # `application/octet-stream` rather than a guess from the extension:
        # the attachment disposition forces a save either way, and an
        # extension-to-type map can only ever be wrong.
        return Response(
            content=blob["raw_content"],
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": _content_disposition(
                    blob["filename"] or f"{name}-v{blob['version']}.txt"
                )
            },
        )
    return Response(
        content=(blob["decklist"] or "").encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": _content_disposition(f"{name}-v{blob['version']}.txt")
        },
    )


@router.post(
    "/{deck_id}/versions/{version}/restore",
    summary="Restore a past version",
    description=(
        "Re-applies an old version by APPENDING its text as a new version "
        "with `source = 'restore'`. History is never rewritten or truncated, "
        "so the restore itself shows up as an entry and the version you "
        "restored from stays exactly where it was.\n\n"
        "Restoring the version that is already current is a no-op returning "
        "it unchanged (`version_created: false`). Errors: 404 `not_found`. "
        "Auto-registered as MCP tool `restore_deck_version`."
    ),
    responses={404: _ERROR_RESPONSE},
)
async def restore_deck_version(
    deck_id: str = _FastPath(
        description="Deck id (uuid)", examples=["11111111-1111-1111-1111-111111111111"]
    ),
    version: int = _FastPath(description="Version number to restore", examples=[1]),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Restore a past version by appending it onto its own branch."""
    if not _is_uuid(deck_id):
        return _scryfall_error(404, "not_found", f"No deck with id {deck_id}")
    async with engine.begin() as conn:
        result = await deck_service.restore_version(conn, deck_id, version)
    if result is None:
        return _scryfall_error(404, "not_found", f"No version {version} for deck {deck_id}")
    row = result.version
    return {
        "object": "deck_version",
        "deck_id": deck_id,
        "restored_from": version,
        "version_created": result.created,
        "version": row["version"],
        "branch": row["branch"],
        "total_cards": row["total_cards"],
        "distinct_cards": row["distinct_cards"],
        "unresolved_count": row["unresolved_count"],
    }


class ForkDeckRequest(BaseModel):
    """Body for ``POST /decks/{deck_id}/fork``."""

    name: str | None = Field(
        default=None,
        description=(
            "Name for the new deck. Defaults to the source deck's name "
            "with ` (fork)` appended."
        ),
        examples=["Budget Iroh"],
    )


@router.post(
    "/{deck_id}/fork",
    summary="Fork a deck into a new, independent deck",
    description=(
        "Creates a NEW deck seeded at version 1 from the source deck's "
        "HEAD — the content it currently shows, which can differ from its "
        "newest version when head is pinned (`POST /decks/{id}/head`) or "
        "when `trunk_branch` has kept it off the branch most recently "
        "imported onto. A fork is for a build you playtest independently "
        "and never reconverge (a `budget` or `pre-rotation` variant) — "
        "exactly what a branch is wrong for and a separate deck is "
        "right for.\n\n"
        "Inherits `format` / `commander` / `companion` / `folder` from the "
        "source. Does NOT inherit `deckbox_url` / `moxfield_url` / "
        "`archidekt_url` — the fork is not that remote deck, and copying "
        "the link would point a second deck at the same remote, risking a "
        "later sync overwriting the wrong list. Does NOT copy version "
        "history — the fork starts its own timeline at version 1, with "
        "`source = 'fork'` on that version. The source deck is completely "
        "untouched: same version count, same head, same coverage.\n\n"
        "Errors: 404 `not_found` for an unknown or malformed source id. "
        "Auto-registered as MCP tool `fork_deck`."
    ),
    responses={404: _ERROR_RESPONSE},
)
async def fork_deck(
    req: ForkDeckRequest,
    deck_id: str = _FastPath(
        description="Source deck id (uuid)", examples=["11111111-1111-1111-1111-111111111111"]
    ),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Fork a deck into a new, independent deck seeded from its HEAD."""
    if not _is_uuid(deck_id):
        return _scryfall_error(404, "not_found", f"No deck with id {deck_id}")
    async with engine.begin() as conn:
        forked = await deck_service.fork_deck(conn, deck_id, name=req.name)
    if forked is None:
        return _scryfall_error(404, "not_found", f"No deck with id {deck_id}")
    return forked


class SetHeadRequest(BaseModel):
    """Body for ``POST /decks/{deck_id}/head``."""

    version: int | None = Field(
        default=None,
        description=(
            "Version number to promote to head and pin. `null` (or the "
            "field omitted) CLEARS an existing pin instead and resumes "
            "auto-follow onto the newest version across every branch — "
            "the un-pin control D19.37 added; there is no third value."
        ),
        examples=[3],
    )


@router.post(
    "/{deck_id}/head",
    summary="Promote a version to head, or clear the pin",
    description=(
        "`{\"version\": N}` explicitly moves the deck's head pointer to "
        "that version and sets `head_pinned = true`. From this point, "
        "imports onto OTHER branches no longer move head automatically — "
        "every 'current content' read (`GET /decks/{id}`, `GET /decks`, "
        "`GET /decks/{id}/cards`, the effective commander/companion) "
        "follows this pointer instead of whatever version was written "
        "most recently. The derived `deck_cards` index is rebuilt in the "
        "same transaction, since promoting changes what 'current content' "
        "means.\n\n"
        "`{\"version\": null}` (or an omitted `version`) CLEARS the pin "
        "instead and resumes auto-follow onto the newest version across "
        "every branch — without this, a pin is permanent and every later "
        "import keeps reporting success while the deck silently stops "
        "changing. `deck_cards` is rebuilt onto that resolved version in "
        "the same transaction, and a subsequent import moves head again. "
        "Errors: 404 `not_found` for an unknown deck, an unknown "
        "`version`, or an un-pin request against a deck with no versions "
        "at all. "
        "Auto-registered as MCP tool `set_deck_head`."
    ),
    responses={404: _ERROR_RESPONSE},
)
async def set_deck_head(
    req: SetHeadRequest,
    deck_id: str = _FastPath(
        description="Deck id (uuid)", examples=["11111111-1111-1111-1111-111111111111"]
    ),
    engine: _Engine = Depends(get_read_engine),
) -> Any:
    """Promote a version to head and pin it, or clear an existing pin."""
    if not _is_uuid(deck_id):
        return _scryfall_error(404, "not_found", f"No deck with id {deck_id}")
    async with engine.begin() as conn:
        row = await deck_service.set_head(conn, deck_id, req.version)
    if row is None:
        detail = (
            f"No version {req.version} for deck {deck_id}"
            if req.version is not None
            else f"No deck with id {deck_id}"
        )
        return _scryfall_error(404, "not_found", detail)
    return {
        "object": "deck_head",
        "deck_id": deck_id,
        "version": row["version"],
        "branch": row["branch"],
        "head_pinned": row["head_pinned"],
    }
