"""Stored-deck persistence (D19.4).

`decks` holds identity and user-owned metadata; `deck_versions` holds
every state the decklist has ever been in, and the CURRENT decklist is
simply the highest-numbered version (D19.1). There is no
`decks.decklist` column, so nothing here can leave a deck's content and
its history disagreeing.

Content is stored as decklist TEXT rather than resolved `(oracle_id,
qty)` rows: every `/decks/*` endpoint already consumes a decklist
string, so text needs no resolution at read time and cannot rot when a
refresh changes what a name resolves to.

ponytail: no ORM, no repository class, no unit-of-work. Module-level
async functions taking a connection, exactly like `inventory/service.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from biblioplex.decklist.diff import diff_qty_maps
from biblioplex.decklist.resolve import resolve_names
from biblioplex.decklist.sections import parse_sections, subtract_entries

_Connection = Any

__all__ = [
    "AppendResult",
    "MAX_NAME_LEN",
    "MUTABLE_FIELDS",
    "VALID_BRANCHES",
    "VALID_SOURCES",
    "append_version",
    "create_deck",
    "decklist_counts",
    "deck_exists",
    "delete_deck",
    "effective_commander",
    "effective_companion",
    "completion_plan",
    "fork_deck",
    "get_deck",
    "get_version",
    "get_version_blob",
    "list_branches",
    "list_decks",
    "list_versions",
    "restore_version",
    "set_head",
    "update_deck",
]

MAX_NAME_LEN = 200
DEFAULT_DECK_NAME = "Untitled deck"

# Mirrors `schema.sql`'s `deck_versions.branch` CHECK exactly (D19.33) — a
# branch typo is a 422 at the router, not a phantom label or a DB error.
VALID_BRANCHES = ("main", "deckbox", "moxfield", "archidekt")

# Mirrors `schema.sql`'s `deck_versions.source` CHECK (D19.1) — D19.37's
# `POST /decks/:id/versions` accepts an override so a scripted caller can
# label what it just wrote, defaulting to "upload" since the route always
# carries a file. `fork` (D19.41) is `fork_deck`'s own version 1 — no
# router path lets a caller choose it via the override.
VALID_SOURCES = ("upload", "paste", "url", "edit", "restore", "fork")

# Whitelist for the dynamic UPDATE. Column names are only ever taken from
# this tuple — never from caller input — so the interpolation below cannot
# become an injection point; the VALUES stay bound parameters.
MUTABLE_FIELDS = (
    "name",
    "format",
    "commander",
    "companion",
    "notes",
    "deckbox_url",
    "moxfield_url",
    "archidekt_url",
    "folder",
    # D19.38: the one branch allowed to move head. `None` (the default,
    # and the only spelling `PATCH` can send to blank it — see the
    # nullable-blanking convention `folder` already established) means
    # "follow every import", i.e. today's behaviour.
    "trunk_branch",
)

# `head_version` / `head_pinned` are NOT in `MUTABLE_FIELDS` — they are
# never set by the generic PATCH, only by `append_version`'s auto-follow or
# the dedicated `set_head` promote route (D19.34).
_DECK_COLUMNS = (
    "id", *MUTABLE_FIELDS, "head_version", "head_pinned", "created_at", "updated_at"
)
_DECK_RETURNING = ", ".join(_DECK_COLUMNS)

# `decklist` is fetched with the version because callers need it to derive
# the effective commander; the LIST payload still omits it (see list_decks).
_VERSION_COLUMNS = (
    "id",
    "version",
    "branch",
    "message",
    "decklist",
    "source",
    "filename",
    "size_bytes",
    "total_cards",
    "distinct_cards",
    "unresolved_count",
    "created_at",
)
_VERSION_RETURNING = ", ".join(_VERSION_COLUMNS)

# The history listing selects neither `decklist` nor `raw_content`: browsing
# a deck's past must not drag every stored file out of the DB. Same call as
# `uploads.list_uploads` (D18.17).
_VERSION_SUMMARY = (
    "id, version, branch, message, source, filename, size_bytes, "
    "total_cards, distinct_cards, unresolved_count, created_at"
)


def decklist_counts(decklist: str) -> tuple[int, int, list[str]]:
    """Return ``(total_cards, distinct_cards, distinct_names)``.

    Spans ``main + sideboard`` ONLY. `parse_sections` is not a partition
    (D19.10): commander and companion entries are already inside `main`,
    so adding those buckets back in counts them twice and reports a
    101-card Commander deck. Distinctness is case-folded so `Sol Ring` and
    `SOL RING` are one card, matching how resolution treats them.
    """
    s = parse_sections(decklist)
    entries = [*s.main, *s.sideboard]
    total = sum(e.qty for e in entries)
    by_lowered: dict[str, str] = {}
    for e in entries:
        by_lowered.setdefault(e.name.lower(), e.name)
    return total, len(by_lowered), list(by_lowered.values())


def effective_commander(override: str | None, decklist: str | None) -> str | None:
    """`decks.commander` when set, else the decklist's Commander section.

    The override exists because every Deckbox export lacks a Commander
    section entirely, so derivation alone would leave those decks unable
    to drive `/decks/legality` or `/decks/bracket`.

    Partners are joined with ` // `. That is lossy in principle — a card
    name can contain ` // ` — but a commander must be a legendary
    creature, and no split card is one. See the M19 drift entry.
    """
    if override and override.strip():
        return override.strip()
    if not decklist:
        return None
    names = [e.name for e in parse_sections(decklist).commander]
    return " // ".join(names) or None


def effective_companion(override: str | None, decklist: str | None) -> str | None:
    """`decks.companion` when set, else the decklist's Companion section.

    Identical override semantics to `effective_commander`, and for the same
    reason: no reference export in `data/example_deck_exports/` carries a
    `Companion` section, so derivation alone would leave the field
    permanently null. A deck has at most one companion, but the bucket is a
    list for symmetry — joined the same way if a file ever lists two.
    """
    if override and override.strip():
        return override.strip()
    if not decklist:
        return None
    names = [e.name for e in parse_sections(decklist).companion]
    return " // ".join(names) or None


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _version_payload(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "version": row["version"],
        "branch": row["branch"],
        "message": row["message"],
        "source": row["source"],
        "filename": row["filename"],
        "size_bytes": row["size_bytes"],
        "total_cards": row["total_cards"],
        "distinct_cards": row["distinct_cards"],
        "unresolved_count": row["unresolved_count"],
        "created_at": _iso(row["created_at"]),
    }


def _deck_payload(
    deck: Any,
    version: Any,
    *,
    version_created: bool | None = None,
    diff_bases: dict[str, int | None] | None = None,
    imported_version: int | None = None,
    unresolved_now: int | None = None,
) -> dict[str, Any]:
    """Assemble one deck's response.

    `version` here is always the HEAD version row (D19.34) — never merely
    "the newest one" — so `decklist` / `commander` / `total_cards` etc.
    always describe what the deck's head currently points at. When a write
    just happened, `imported_version` / `version_created` / `diff_bases`
    report what that write actually did, which can differ from head when
    the write landed on a pinned deck's non-head branch.
    """
    decklist = version["decklist"] if version is not None else None
    out: dict[str, Any] = {
        "object": "deck",
        "id": str(deck["id"]),
        "name": deck["name"],
        "format": deck["format"],
        "commander": effective_commander(deck["commander"], decklist),
        "commander_override": deck["commander"],
        "companion": effective_companion(deck["companion"], decklist),
        "companion_override": deck["companion"],
        "notes": deck["notes"],
        "deckbox_url": deck["deckbox_url"],
        "moxfield_url": deck["moxfield_url"],
        "archidekt_url": deck["archidekt_url"],
        "folder": deck["folder"],
        "trunk_branch": deck["trunk_branch"],
        "head_pinned": bool(deck["head_pinned"]),
        "created_at": _iso(deck["created_at"]),
        "updated_at": _iso(deck["updated_at"]),
        "decklist": decklist,
        "version": version["version"] if version is not None else None,
        "branch": version["branch"] if version is not None else None,
        "total_cards": version["total_cards"] if version is not None else 0,
        "distinct_cards": version["distinct_cards"] if version is not None else 0,
        # D19.47: the CURRENT count from `deck_cards` when the caller
        # supplies it, so the detail header and the list of names
        # beneath it read the same rows. The per-version value stays
        # the historical record of how well one import matched, and is
        # still served unchanged by GET /decks/{id}/versions.
        "unresolved_count": (
            unresolved_now
            if unresolved_now is not None
            else (version["unresolved_count"] if version is not None else 0)
        ),
    }
    if version_created is not None:
        out["version_created"] = version_created
        out["imported_version"] = imported_version
    if diff_bases is not None:
        out["diff_bases"] = diff_bases
    return out


async def _max_version_number(conn: _Connection, deck_id: str) -> int | None:
    """Highest version NUMBER a deck has, across every branch, or None.

    Version numbers are global per deck (D19.33 — a branch is a label plus
    a filter, not a namespace), so the next version number is never scoped
    to one branch even though dedup and diff bases are.
    """
    res = await conn.execute(
        text(
            "SELECT MAX(version) AS max_version FROM deck_versions "
            "WHERE deck_id = CAST(:deck_id AS uuid)"
        ),
        {"deck_id": deck_id},
    )
    value = res.mappings().one()["max_version"]
    return int(value) if value is not None else None


async def _branch_latest_version(conn: _Connection, deck_id: str, branch: str) -> Any:
    """Latest version ON ONE BRANCH. Deliberately NOT head-aware.

    Used for per-branch dedup and for diff bases, both of which must
    compare against what a given branch last looked like — independent of
    wherever the deck's head pointer currently sits.
    """
    res = await conn.execute(
        text(
            f"SELECT {_VERSION_RETURNING} FROM deck_versions "  # noqa: S608
            "WHERE deck_id = CAST(:deck_id AS uuid) AND branch = :branch "
            "ORDER BY version DESC LIMIT 1"
        ),
        {"deck_id": deck_id, "branch": branch},
    )
    return res.mappings().one_or_none()


async def _head_version_row(
    conn: _Connection, deck_id: str, head_version_number: int | None
) -> Any:
    """The version row a deck's HEAD currently points at.

    `head_version_number` is `decks.head_version` for this deck — pass
    None for a deck that has never been pinned or auto-followed under
    D19.34, which falls back to the newest version (D19.33's back-compat
    default for pre-existing decks).

    This — NOT a bare "ORDER BY version DESC LIMIT 1" — is what every
    "current content" read must go through: `get_deck`, `update_deck`'s
    metadata-only path, `list_decks`'s LATERAL and
    `rebuild_all_deck_cards`'s LATERAL. Reading the newest row directly
    would silently show the wrong branch's content on a deck whose head is
    pinned somewhere else (D19.34's trap).
    """
    # HEAD-AWARE "latest" definition
    res = await conn.execute(
        text(
            f"SELECT {_VERSION_RETURNING} FROM deck_versions "  # noqa: S608
            "WHERE deck_id = CAST(:deck_id AS uuid) "
            "AND version = COALESCE("
            ":head_version, "
            "(SELECT MAX(version) FROM deck_versions WHERE deck_id = CAST(:deck_id AS uuid))"
            ")"
        ),
        {"deck_id": deck_id, "head_version": head_version_number},
    )
    return res.mappings().one_or_none()


async def _diff_bases(
    conn: _Connection, deck_id: str, branch: str, branch_latest: Any
) -> dict[str, int | None]:
    """Pre-write diff bases for D19.36's UI: ``{main, branch}``.

    Both are captured from what each branch looked like BEFORE this
    write — not from head, which has just auto-moved onto the new version
    by the time a caller would read it, making a head-relative diff always
    empty. `main` is branch `main`'s own latest version regardless of which
    branch is being written; `branch` is this branch's own previous latest.
    They coincide when writing onto `main` itself.
    """
    branch_version = branch_latest["version"] if branch_latest is not None else None
    if branch == "main":
        return {"main": branch_version, "branch": branch_version}
    main_latest = await _branch_latest_version(conn, deck_id, "main")
    main_version = main_latest["version"] if main_latest is not None else None
    return {"main": main_version, "branch": branch_version}


@dataclass(frozen=True)
class AppendResult:
    """Outcome of `append_version` — the write, and where head ended up."""

    version: Any
    created: bool
    head_version: int
    head_pinned: bool
    diff_bases: dict[str, int | None]


async def append_version(
    conn: _Connection,
    deck_id: str,
    decklist: str,
    *,
    source: str,
    branch: str = "main",
    message: str | None = None,
    filename: str | None = None,
    raw_content: bytes | None = None,
) -> AppendResult:
    """Append one version onto one branch. See `AppendResult`.

    Dedup is PER-BRANCH: a decklist byte-identical to this branch's own
    latest is a NO-OP returning that row with ``created=False`` — an
    unchanged Moxfield re-import right after a Deckbox import must not look
    changed just because the two branches disagree.

    Head auto-follow: when the deck is not pinned, head moves to the new
    version. When it IS pinned, head stays put. The derived `deck_cards`
    index is rebuilt ONLY when head actually moves onto this write — D19.19
    rebuilds it in the write's own transaction, which is safe only when the
    newest row IS current content; once head can lag, rebuilding
    unconditionally would silently swing `deck_cards` (and every
    `coverage_pct` / `missing_usd` derived from it) onto a version the deck
    is not on. A deduped write never moves head and never rebuilds — there
    is nothing new to move to.

    D19.38 `trunk_branch`: names the ONE branch allowed to move head, so a
    deck synced from two services stops flipping its current content with
    sync order. `NULL` means "follow every import" (back-compat). A write
    onto a non-trunk branch takes the EXACT blocked path a pinned deck
    already takes — no head move, no `deck_cards` rebuild — so there is
    only ever one "don't rebuild" code path to trust. `head_pinned` still
    wins: a pinned deck ignores `trunk_branch` entirely.
    """
    if branch not in VALID_BRANCHES:
        raise ValueError(f"branch must be one of {', '.join(VALID_BRANCHES)}")

    head_row = (
        await conn.execute(
            text(
                "SELECT head_version, head_pinned, trunk_branch FROM decks "
                "WHERE id = CAST(:deck_id AS uuid)"
            ),
            {"deck_id": deck_id},
        )
    ).mappings().one()
    head_pinned = bool(head_row["head_pinned"])
    trunk_branch = head_row["trunk_branch"]
    current_head_version = head_row["head_version"]
    follows_head = not head_pinned and (trunk_branch is None or branch == trunk_branch)

    branch_latest = await _branch_latest_version(conn, deck_id, branch)
    diff_bases = await _diff_bases(conn, deck_id, branch, branch_latest)

    if branch_latest is not None and branch_latest["decklist"] == decklist:
        if current_head_version is None:
            current_head_version = await _max_version_number(conn, deck_id)
        return AppendResult(
            version=branch_latest,
            created=False,
            head_version=current_head_version,
            head_pinned=head_pinned,
            diff_bases=diff_bases,
        )

    total, distinct, names = decklist_counts(decklist)
    unresolved = len((await resolve_names(conn, names)).unresolved)
    version = (await _max_version_number(conn, deck_id) or 0) + 1

    res = await conn.execute(
        text(
            "INSERT INTO deck_versions "  # noqa: S608
            "(deck_id, version, branch, message, decklist, raw_content, "
            " filename, source, size_bytes, total_cards, distinct_cards, "
            " unresolved_count) "
            "VALUES (CAST(:deck_id AS uuid), :version, :branch, :message, "
            " :decklist, :raw_content, :filename, :source, :size_bytes, "
            " :total_cards, :distinct_cards, :unresolved_count) "
            f"RETURNING {_VERSION_RETURNING}"
        ),
        {
            "deck_id": deck_id,
            "version": version,
            "branch": branch,
            "message": message,
            "decklist": decklist,
            "raw_content": raw_content,
            "filename": filename,
            "source": source,
            # Sized by the bytes actually stored: the original file when
            # there is one, the canonical text otherwise.
            "size_bytes": len(raw_content) if raw_content is not None
            else len(decklist.encode("utf-8")),
            "total_cards": total,
            "distinct_cards": distinct,
            "unresolved_count": unresolved,
        },
    )
    new_row = res.mappings().one()

    if not follows_head:
        # Pinned, or landing on a non-trunk branch: head stays exactly
        # where it is. Nothing new to rebuild — the trap this deliverable
        # (and D19.38 after it) exists to close. Shared path: only what is
        # RETURNED differs between the two reasons, never the SQL taken.
        return AppendResult(
            version=new_row,
            created=True,
            head_version=current_head_version,
            head_pinned=head_pinned,
            diff_bases=diff_bases,
        )

    await conn.execute(
        text(
            "UPDATE decks SET head_version = :version "
            "WHERE id = CAST(:deck_id AS uuid)"
        ),
        {"version": version, "deck_id": deck_id},
    )
    # The derived index tracks HEAD, and head just moved onto this version,
    # so it is rebuilt in the same transaction that writes it. Rolling back
    # the version rolls back the index and the head move with it.
    await rebuild_deck_cards(conn, deck_id, decklist)
    return AppendResult(
        version=new_row,
        created=True,
        head_version=version,
        head_pinned=False,
        diff_bases=diff_bases,
    )


async def set_head(conn: _Connection, deck_id: str, version: int | None) -> Any | None:
    """Promote a version to head and PIN it, or (`version=None`) clear the
    pin and resume auto-follow. None when the target (version, or the deck
    itself for an un-pin) is unknown.

    Explicit promotion is also a head move, so it carries the identical
    obligation `append_version` does: rebuild the derived `deck_cards`
    index in the SAME transaction, because head's content just changed.

    Un-pinning (D19.37) resumes auto-follow by setting `head_version` back
    to `NULL` — D19.33's back-compat meaning of "follow the newest version
    across every branch" — rather than leaving it at the just-cleared pin,
    which would silently keep pointing at the same content forever and
    look identical to still being pinned. `deck_cards` is rebuilt onto
    whatever that resolves to right now, reusing `_head_version_row`
    (the one function the module trusts to define "current content") so
    this never invents a fourth ad-hoc "latest" read.
    """
    if version is None:
        row = await _head_version_row(conn, deck_id, None)
        if row is None:
            return None
        await conn.execute(
            text(
                "UPDATE decks SET head_version = NULL, head_pinned = false "
                "WHERE id = CAST(:deck_id AS uuid)"
            ),
            {"deck_id": deck_id},
        )
        await rebuild_deck_cards(conn, deck_id, row["decklist"])
        return {"version": row["version"], "branch": row["branch"], "head_pinned": False}

    row = (
        await conn.execute(
            text(
                "SELECT version, branch, decklist FROM deck_versions "
                "WHERE deck_id = CAST(:deck_id AS uuid) AND version = :version"
            ),
            {"deck_id": deck_id, "version": version},
        )
    ).mappings().one_or_none()
    if row is None:
        return None

    await conn.execute(
        text(
            "UPDATE decks SET head_version = :version, head_pinned = true "
            "WHERE id = CAST(:deck_id AS uuid)"
        ),
        {"version": version, "deck_id": deck_id},
    )
    await rebuild_deck_cards(conn, deck_id, row["decklist"])
    return {"version": row["version"], "branch": row["branch"], "head_pinned": True}



# D19.16 folders. A folder is a path string on the deck, never its own row,
# so "the folder exists" only ever means "some deck's path starts with it".
MAX_FOLDER_LEN = 256


def normalize_folder(raw: str | None) -> str | None:
    """Canonicalise a folder path, or None for unfiled.

    Trims the whole string and every segment, collapses repeated
    separators, drops leading/trailing ones, and maps anything that
    normalises to nothing onto None. Case is preserved deliberately:
    folding it would surprise anyone who capitalises on purpose, and the
    priced cost — `Commander` and `commander` being two folders — is what
    D19.17's subtree rename exists to repair.

    Raises ValueError on control characters (a newline would break any
    tree the UI renders) or an over-long path.
    """
    if raw is None:
        return None
    if any(ch < " " or ch == "\x7f" for ch in raw):
        raise ValueError("folder must not contain control characters")
    segments = [seg.strip() for seg in raw.strip().split("/")]
    path = "/".join(seg for seg in segments if seg)
    if not path:
        return None
    if len(path) > MAX_FOLDER_LEN:
        raise ValueError(f"folder too long (max {MAX_FOLDER_LEN})")
    return path


def folder_like_prefix(folder: str) -> str:
    """LIKE pattern matching a folder's descendants, and only those.

    Anchored on a trailing slash so `Commander` cannot match `CommanderX`
    — a bare `folder || '%'` silently sweeps in unrelated sibling folders.
    `%`, `_` and the escape character itself are escaped, because they are
    legal in a real folder name (`50%_budget`) and would otherwise turn one
    subtree query into a full-table match. Callers must pair this with
    ``ESCAPE '\\'``.
    """
    escaped = folder.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}/%"


async def list_folders(conn: _Connection) -> dict[str, Any]:
    """Every folder path with its deck counts, ancestors included.

    Ancestors are DERIVED, not stored: a deck filed at `Commander/Budget`
    means `Commander` is a real node even when no deck sits directly in
    it. Without that the UI gets a leaf whose parent is missing and cannot
    build a tree. `deck_count` is decks directly in the folder;
    `total_deck_count` includes every descendant.
    """
    rows = (
        await conn.execute(
            text(
                "SELECT folder, COUNT(*) AS deck_count FROM decks "
                "WHERE folder IS NOT NULL GROUP BY folder"
            )
        )
    ).mappings().all()

    direct: dict[str, int] = {}
    total: dict[str, int] = {}
    for row in rows:
        path = str(row["folder"])
        count = int(row["deck_count"])
        direct[path] = direct.get(path, 0) + count
        segments = path.split("/")
        for depth in range(len(segments)):
            ancestor = "/".join(segments[: depth + 1])
            total[ancestor] = total.get(ancestor, 0) + count
            direct.setdefault(ancestor, 0)

    unfiled_row = (
        await conn.execute(
            text("SELECT COUNT(*) AS unfiled FROM decks WHERE folder IS NULL")
        )
    ).mappings().one_or_none()

    return {
        "object": "list",
        "data": [
            {
                "path": path,
                "name": path.split("/")[-1],
                "depth": path.count("/"),
                "deck_count": direct[path],
                "total_deck_count": total.get(path, 0),
            }
            for path in sorted(direct)
        ],
        "unfiled_count": int(unfiled_row["unfiled"]) if unfiled_row else 0,
    }



async def rename_folder(
    conn: _Connection, old: str | None, new: str | None
) -> int:
    """Move a folder subtree. Returns how many decks were re-filed.

    `new` of None (or anything normalising to nothing) UNFILES the
    subtree, which is also how a folder gets deleted — there is no folder
    row to remove.

    The WHERE shape is the point. Matching is exact-OR-slash-anchored
    because the obvious `folder LIKE :old || '%'` also matches
    `CommanderX`, silently re-filing an unrelated sibling that merely
    shares a name prefix.

    Descendants are rewritten by OFFSET, not by string replacement: a
    blind replace would rewrite every occurrence, turning
    `Commander/x/Commander` into `Legacy/x/Legacy` instead of
    `Legacy/x/Commander`. Taking the remainder from `len(old)` touches
    only the prefix.

    `updated_at` is deliberately NOT moved. `update_deck` touches it so
    the library's newest-touched-first ordering tracks real edits; a bulk
    reorganisation is not per-deck activity, and bumping every row would
    reshuffle the whole library for a purely organisational change.
    """
    source = normalize_folder(old)
    if source is None:
        # Without this guard the UPDATE would match every unfiled deck.
        raise ValueError("folder to rename is required")
    target = normalize_folder(new)

    assignment = (
        "folder = NULL"
        if target is None
        else "folder = :new || substr(folder, :old_len + 1)"
    )
    params: dict[str, Any] = {
        "old": source,
        "old_prefix": folder_like_prefix(source),
    }
    if target is not None:
        params["new"] = target
        params["old_len"] = len(source)

    result = await conn.execute(
        text(
            f"UPDATE decks SET {assignment} "  # noqa: S608
            "WHERE folder = :old "
            "   OR folder LIKE :old_prefix ESCAPE '\\'"
        ),
        params,
    )
    return int(getattr(result, "rowcount", 0) or 0)



@dataclass(frozen=True)
class DeckCardRow:
    """One row of the derived `deck_cards` index."""

    section: str
    card_name: str
    qty: int


def deck_card_rows(decklist: str) -> list[DeckCardRow]:
    """Fold a decklist into PARTITIONED rows: commander, main, sideboard.

    `parse_sections` is deliberately not a partition — commander and
    companion sit inside `main` (D19.10) — so both are subtracted back out
    of the mainboard here. Without that, `SUM(qty)` over the index reports
    a 101-card Commander deck and every coverage percentage computed from
    it is wrong. Subtraction is by quantity, so a commander that is also a
    legal singleton in the 99 keeps its other copy.

    Duplicate lines fold together: the index is keyed on meaning, not on
    how many times the export happened to mention a card.
    """
    sections = parse_sections(decklist)
    body = subtract_entries(sections.main, sections.commander, sections.companion)

    out: list[DeckCardRow] = []
    for section, entries in (
        ("commander", [*sections.commander, *sections.companion]),
        ("main", body),
        ("sideboard", sections.sideboard),
    ):
        folded: dict[str, tuple[str, int]] = {}
        for entry in entries:
            key = entry.name.lower()
            name, qty = folded.get(key, (entry.name, 0))
            folded[key] = (name, qty + entry.qty)
        out += [
            DeckCardRow(section=section, card_name=name, qty=qty)
            for name, qty in folded.values()
        ]
    return out


def _folded_qty_map(decklist: str) -> dict[str, tuple[str, int]]:
    """`deck_card_rows()` folded into `diff_qty_maps`'s input shape.

    Same `lowered_name -> (display_name, qty)` shape `routers.decks`'
    `_decklist_qty_map` builds from raw text, but sourced from the
    PARTITION-AWARE fold (D19.10's commander/companion subtraction) so a
    branch comparison can never disagree with the persisted `deck_cards`
    index or D19.35's live per-version read — all three go through
    `deck_card_rows`. Sections collapse here: `list_branches` only needs
    per-card totals, not which section each half sits in.
    """
    out: dict[str, tuple[str, int]] = {}
    for row in deck_card_rows(decklist):
        key = row.card_name.lower()
        name, qty = out.get(key, (row.card_name, 0))
        out[key] = (name, qty + row.qty)
    return out


async def list_branches(conn: _Connection, deck_id: str) -> dict[str, Any] | None:
    """One row per branch with any version, compared against HEAD.

    "Are my synced copies in sync?" — with zero fetch, since every branch
    head is already stored locally (D19.33/34). ONE query for every
    branch's own latest version, served directly by
    `deck_versions_branch_idx (deck_id, branch, version DESC)`, then pure
    Python over at most `len(VALID_BRANCHES)` decklists.

    **Text is never compared.** A Deckbox export and a Moxfield export of
    the identical deck differ byte-for-byte (line order, comments, tail
    annotations) — comparing raw text would report permanent divergence.
    Both sides fold through `_folded_qty_map` (i.e. `deck_card_rows`) and
    the SAME classifier `POST /decks/diff` runs (`diff_qty_maps`), so this
    cannot silently drift from either the persisted card table or the
    stateless diff endpoint.

    The branch HEAD currently sits on is flagged (`is_head`) rather than
    diffed against itself — trivially zero, and skipping the fold avoids
    a redundant `deck_card_rows` call on every request. `differs` counts
    are asymmetric by design: `added` = cards that branch has and HEAD
    does not, `removed` = cards HEAD has and that branch does not.

    Returns `None` for an unknown deck.
    """
    deck_row = (
        await conn.execute(
            text("SELECT head_version FROM decks WHERE id = CAST(:id AS uuid)"),
            {"id": deck_id},
        )
    ).mappings().one_or_none()
    if deck_row is None:
        return None

    head_ver = await _head_version_row(conn, deck_id, deck_row["head_version"])
    if head_ver is None:
        # Unreachable in practice — a deck always has at least version 1
        # (D19.4) — but a deck row with no versions is "nothing to compare"
        # rather than a 404, since the deck itself is real.
        return {"object": "list", "deck_id": deck_id, "data": []}

    tips = (
        (
            await conn.execute(
                text(
                    "SELECT DISTINCT ON (branch) branch, version, created_at, decklist "
                    "FROM deck_versions WHERE deck_id = CAST(:deck_id AS uuid) "
                    "ORDER BY branch, version DESC"
                ),
                {"deck_id": deck_id},
            )
        )
        .mappings()
        .all()
    )

    head_map = _folded_qty_map(head_ver["decklist"] or "")
    branch_order = {b: i for i, b in enumerate(VALID_BRANCHES)}
    rows: list[dict[str, Any]] = []
    for tip in sorted(tips, key=lambda t: branch_order.get(t["branch"], len(VALID_BRANCHES))):
        is_head = tip["branch"] == head_ver["branch"] and tip["version"] == head_ver["version"]
        if is_head:
            differs = {"added": 0, "removed": 0, "changed": 0}
        else:
            tip_map = _folded_qty_map(tip["decklist"] or "")
            diff = diff_qty_maps(head_map, tip_map)
            differs = {
                "added": len(diff["added"]),
                "removed": len(diff["removed"]),
                "changed": len(diff["qty_changed"]),
            }
        rows.append({
            "branch": tip["branch"],
            "version": tip["version"],
            "updated_at": _iso(tip["created_at"]),
            "is_head": is_head,
            "differs": differs,
        })
    return {"object": "list", "deck_id": deck_id, "data": rows}


async def rebuild_deck_cards(
    conn: _Connection, deck_id: str, decklist: str
) -> int:
    """Regenerate one deck's derived rows. Returns rows written.

    Delete-then-insert, never an upsert: without the delete a re-sync
    stacks duplicates forever (the same trap D18.2's merge path had to
    handle), and an emptied deck must not keep its old rows.

    Names resolve inline via a LATERAL lookup rather than a round trip per
    card. LATERAL … LIMIT 1 rather than a plain join because `cards` can
    hold more than one row for a name (reversible cards split per face,
    §3.4) and a plain join would multiply the deck's rows.
    """
    await conn.execute(
        text("DELETE FROM deck_cards WHERE deck_id = CAST(:deck_id AS uuid)"),
        {"deck_id": deck_id},
    )
    rows = deck_card_rows(decklist)
    if not rows:
        return 0

    params: dict[str, Any] = {"deck_id": deck_id}
    values: list[str] = []
    for i, row in enumerate(rows):
        params[f"s{i}"], params[f"n{i}"], params[f"q{i}"] = (
            row.section,
            row.card_name,
            row.qty,
        )
        values.append(f"(:s{i}, :n{i}, :q{i})")

    await conn.execute(
        text(
            "INSERT INTO deck_cards (deck_id, section, card_name, oracle_id, qty) "  # noqa: S608
            "SELECT CAST(:deck_id AS uuid), v.section, v.card_name, c.oracle_id, v.qty "
            f"FROM (VALUES {', '.join(values)}) AS v(section, card_name, qty) "
            "LEFT JOIN LATERAL ("
            "  SELECT oracle_id FROM cards WHERE lower(name) = lower(v.card_name) LIMIT 1"
            ") c ON TRUE"
        ),
        params,
    )
    return len(rows)



async def rebuild_all_deck_cards(conn: _Connection) -> int:
    """Regenerate the derived index for every deck. Returns decks rebuilt.

    Two jobs: backfilling decks that predate the index, and repairing
    resolution drift after a card refresh — a name that resolved to no
    `oracle_id` last week may resolve now, and coverage silently
    under-reports until the row is rewritten.

    Cheap because it is O(decks), not O(cards): each deck is one DELETE
    plus one INSERT…SELECT. Safe to run repeatedly — the text is the
    source of truth, so this only ever recomputes.

    Reads each deck's HEAD content, not merely its newest version (D19.34)
    — otherwise a refresh-triggered rebuild would silently overwrite a
    pinned deck's index with a branch it is not currently on.
    """
    rows = (
        await conn.execute(
            text(
                # HEAD-AWARE "latest" definition
                "SELECT d.id, v.decklist FROM decks d "
                "LEFT JOIN LATERAL ("
                "  SELECT decklist FROM deck_versions WHERE deck_id = d.id "
                "  AND version = COALESCE(d.head_version, "
                "    (SELECT MAX(version) FROM deck_versions WHERE deck_id = d.id)"
                "  )"
                ") v ON TRUE"
            )
        )
    ).mappings().all()

    rebuilt = 0
    for row in rows:
        await rebuild_deck_cards(conn, str(row["id"]), row["decklist"] or "")
        rebuilt += 1
    return rebuilt



_DECK_CARDS_SQL = (
    "SELECT dc.section, dc.card_name, dc.qty, dc.oracle_id, "
    "       c.id, c.name, c.type_line, c.mana_cost, c.cmc, c.price_usd, "
    "       COALESCE(inv.owned, 0) AS qty_owned, "
    "       (wl.n IS NOT NULL) AS on_wishlist "
    "FROM deck_cards dc "
    "LEFT JOIN LATERAL ("
    "  SELECT id, name, type_line, mana_cost, cmc, price_usd "
    "  FROM cards WHERE oracle_id = dc.oracle_id LIMIT 1"
    ") c ON TRUE "
    # SUM, not a row read: `inventory` is one row PER PRINTING (D18.1), so
    # reading a row keeps only one edition's quantity and under-reports a
    # card owned across two sets. This is D18.5's bug, and it is silent.
    "LEFT JOIN LATERAL ("
    "  SELECT SUM(quantity) AS owned FROM inventory "
    "  WHERE oracle_id = dc.oracle_id"
    ") inv ON TRUE "
    "LEFT JOIN LATERAL ("
    "  SELECT 1 AS n FROM wishlist WHERE oracle_id = dc.oracle_id LIMIT 1"
    ") wl ON TRUE "
    "WHERE dc.deck_id = CAST(:deck_id AS uuid) "
    "ORDER BY dc.section, lower(dc.card_name)"
)

#: Table order, not alphabetical — a reader looks for the commander first.
_SECTION_ORDER = {"commander": 0, "main": 1, "sideboard": 2}


async def _live_deck_card_rows(conn: _Connection, decklist: str) -> list[Any]:
    """Enrich one non-HEAD version's decklist with card/inventory data live.

    D19.35's branch-head navigator needs another branch's head card table
    WITHOUT touching the persisted `deck_cards` index — that index only
    ever reflects HEAD (D19.34), and writing to it for a version that is
    merely being VIEWED would silently turn "view" into "promote". Same
    column shape as `_DECK_CARDS_SQL`, sourced from a `VALUES` list and a
    name-based LATERAL rather than the `deck_cards` table — the same
    pattern `rebuild_deck_cards` already uses to resolve names to oracle
    ids without a round trip per card.
    """
    card_rows = deck_card_rows(decklist)
    if not card_rows:
        return []
    params: dict[str, Any] = {}
    values: list[str] = []
    for i, row in enumerate(card_rows):
        params[f"s{i}"], params[f"n{i}"], params[f"q{i}"] = (
            row.section,
            row.card_name,
            row.qty,
        )
        values.append(f"(:s{i}, :n{i}, :q{i})")
    sql = (
        "SELECT v.section, v.card_name, v.qty, c.oracle_id, "  # noqa: S608
        "       c.id, c.name, c.type_line, c.mana_cost, c.cmc, c.price_usd, "
        "       COALESCE(inv.owned, 0) AS qty_owned, "
        "       (wl.n IS NOT NULL) AS on_wishlist "
        f"FROM (VALUES {', '.join(values)}) AS v(section, card_name, qty) "
        "LEFT JOIN LATERAL ("
        "  SELECT id, oracle_id, name, type_line, mana_cost, cmc, price_usd "
        "  FROM cards WHERE lower(name) = lower(v.card_name) LIMIT 1"
        ") c ON TRUE "
        "LEFT JOIN LATERAL ("
        "  SELECT SUM(quantity) AS owned FROM inventory WHERE oracle_id = c.oracle_id"
        ") inv ON TRUE "
        "LEFT JOIN LATERAL ("
        "  SELECT 1 AS n FROM wishlist WHERE oracle_id = c.oracle_id LIMIT 1"
        ") wl ON TRUE "
        "ORDER BY v.section, lower(v.card_name)"
    )
    rows: list[Any] = (await conn.execute(text(sql), params)).mappings().all()
    return rows


def _deck_cards_payload(deck_id: str, rows: list[Any]) -> dict[str, Any]:
    """Shape rows from either `_DECK_CARDS_SQL` or `_live_deck_card_rows`
    into the same `deck_cards` response. One formatter, two sources — the
    HEAD-materialized index for the common case, and a live per-version
    read for D19.35's "view another branch's head" navigator."""
    data: list[dict[str, Any]] = []
    total = owned_total = 0
    missing_usd = 0.0
    unpriced_missing = 0
    total_usd = 0.0
    unpriced_total = 0
    for r in rows:
        qty = int(r["qty"])
        # An unresolved name owns nothing and resolves to no card, but it is
        # still a card in the deck: dropping it would make the table
        # disagree with the deck's own count and inflate coverage.
        owned = min(int(r["qty_owned"] or 0), qty)
        total += qty
        owned_total += owned
        short = max(0, qty - owned)
        price = r["price_usd"]
        # D19.30: deck VALUE spans the whole deck — every section, and cards
        # already owned. Owning a card does not make it worthless; that is
        # what separates this from `missing_usd`. Qty-weighted, so a playset
        # counts four times, not once.
        if price is None:
            unpriced_total += 1
        else:
            total_usd += qty * float(price)
        if short:
            # Priced shortfalls accumulate; unpriced ones are counted so the
            # total is never silently partial (D19.28 author decision).
            if price is None:
                unpriced_missing += 1
            else:
                missing_usd += short * float(price)
        data.append({
            "object": "deck_card",
            "section": r["section"],
            "qty": qty,
            "id": str(r["id"]) if r["id"] else None,
            "oracle_id": str(r["oracle_id"]) if r["oracle_id"] else None,
            "name": r["name"] or r["card_name"],
            "type_line": r["type_line"],
            "mana_cost": r["mana_cost"],
            "cmc": float(r["cmc"]) if r["cmc"] is not None else None,
            "price_usd": float(r["price_usd"]) if r["price_usd"] is not None else None,
            "qty_owned": owned,
            "qty_missing": max(0, qty - owned),
            "on_wishlist": bool(r["on_wishlist"]),
            "resolved": r["oracle_id"] is not None,
        })

    data.sort(key=lambda d: (_SECTION_ORDER.get(d["section"], 9), d["name"].lower()))
    return {
        "object": "deck_cards",
        "deck_id": deck_id,
        "data": data,
        "summary": {
            "total_cards": total,
            "owned_cards": owned_total,
            "missing_cards": total - owned_total,
            "distinct_cards": len(data),
            "unresolved_cards": sum(1 for d in data if not d["resolved"]),
            # An empty deck is fully covered, not a zero-divide.
            "coverage_pct": round(owned_total / total * 100, 2) if total else 100.0,
            "missing_usd": round(missing_usd, 2),
            "unpriced_missing": unpriced_missing,
            # Rounded at the edge, not per card: accumulating rounded cents
            # drifts. No `owned_usd` — it is exactly
            # `total_usd - missing_usd`, and a third field could disagree
            # with the two it is derived from.
            "total_usd": round(total_usd, 2),
            "unpriced_total": unpriced_total,
        },
    }


async def list_deck_cards(
    conn: _Connection, deck_id: str, *, version: int | None = None
) -> dict[str, Any] | None:
    """One deck as table rows with quantity-aware ownership. None if unknown.

    With `version` omitted, reads the DERIVED index (D19.19), so nothing
    re-parses the decklist per request and the sideboard is present —
    `/decks/analyze` reads mainboard only (D19.9) and would silently omit
    it from a deck's own card table. `deck_cards` always reflects HEAD
    (D19.34), so this is the deck's current pointer.

    With `version` given, the table is computed LIVE for that one version
    instead — `deck_cards` has no per-branch or per-version rows, only
    HEAD's. This powers D19.35's branch-head navigator: viewing a branch
    that is not currently HEAD must never write to `deck_cards`, or
    "view" would silently become "promote". Returns `None` for an unknown
    deck OR an unknown version — the router 404s either way.

    Rows are FLAT with a `section` field rather than grouped: sorting the
    whole deck by ownership is the point, and a grouped payload fights it.

    D19.39: when `version` is given, the payload also carries `commander`
    / `companion` — the EFFECTIVE values for that specific version, via
    the same `effective_commander` / `effective_companion` the deck's own
    GET already uses. The override half of those functions reads
    `decks.commander` / `decks.companion`, which are deck-level, not
    per-version — so an explicit override reports identically on every
    branch, and only a DERIVED value (parsed from that version's own
    `Commander`/`Companion` section) tracks the branch being viewed. This
    is the one field this deliverable needed off the deck row that the
    existing `?version=` read did not already carry; omitted on the
    `version is None` path because the caller already has HEAD's values
    from `GET /decks/{id}`.
    """
    deck_row = (
        await conn.execute(
            text("SELECT id, commander, companion FROM decks WHERE id = CAST(:deck_id AS uuid)"),
            {"deck_id": deck_id},
        )
    ).mappings().one_or_none()
    if deck_row is None:
        return None

    commander = companion = None
    if version is None:
        rows = (
            await conn.execute(text(_DECK_CARDS_SQL), {"deck_id": deck_id})
        ).mappings().all()
    else:
        ver = await get_version(conn, deck_id, version)
        if ver is None:
            return None
        rows = await _live_deck_card_rows(conn, ver["decklist"] or "")
        commander = effective_commander(deck_row.get("commander"), ver["decklist"])
        companion = effective_companion(deck_row.get("companion"), ver["decklist"])

    payload = _deck_cards_payload(deck_id, rows)
    if version is not None:
        payload["commander"] = commander
        payload["companion"] = companion
    return payload


# D19.44. Completion key -> WHERE predicate, on the same whitelist
# discipline as `_SORT_EXPRESSIONS` below and for the same reason.
#
# `owned` is clamped per card with LEAST in the CTE (D19.21), so it can
# never exceed `total` and these two predicates really are complements over
# the indexed decks. They are NOT complements over the whole library: a
# deck with no `deck_cards` rows has NULL on both sides, every comparison
# is NULL, and it matches neither — deliberately, since an unindexed deck
# is not known to be complete OR incomplete.
_COMPLETION_PREDICATES = {
    "complete": "cov.total > 0 AND cov.owned >= cov.total",
    "incomplete": "cov.owned < cov.total",
}

# Sort key -> ORDER BY expression. A WHITELIST, never interpolation of
# caller input: `sort` reaches SQL as text, so an unknown key must raise
# rather than be pasted into the statement.
_SORT_EXPRESSIONS = {
    "updated": "d.updated_at",
    "name": "lower(d.name)",
    # The RATIO, not the raw owned count: a fully-built 40-card deck must
    # outrank a 60-of-100 deck. NULLS LAST so a deck with no indexed rows
    # (one predating D19.19, or an empty deck) never outranks a real one.
    "coverage": "CAST(cov.owned AS numeric) / NULLIF(cov.total, 0)",
    # D19.28: what the gap would cost to close.
    "missing_cost": "cov.missing_usd",
}
# `missing_cost` ascends on purpose, unlike coverage: the question is
# "which deck can I finish cheaply?", so cheapest-to-complete leads.
_DEFAULT_DIRECTION = {
    "updated": "DESC", "name": "ASC", "coverage": "DESC", "missing_cost": "ASC",
}

# Inventory is folded ONCE here rather than per deck row. A LATERAL per row
# would do decks x cards index lookups per page; this is one pass over
# `inventory` and one over `deck_cards`.
_COVERAGE_CTE = (
    "WITH inv AS ("
    "  SELECT oracle_id, SUM(quantity) AS owned FROM inventory GROUP BY oracle_id"
    "), cov AS ("
    "  SELECT dc.deck_id, SUM(dc.qty) AS total, "
    # LEAST clamps per card: owning 5 copies of a 1-of contributes 1, not 5.
    # Without it a large collection pushes coverage above 100%.
    "         SUM(LEAST(dc.qty, COALESCE(inv.owned, 0))) AS owned, "
    # D19.28: rough cost of the gap. GREATEST mirrors the LEAST above —
    # owning extras must cost 0, never a negative. `cards.price_usd` is
    # already the CHEAPEST printing across the oracle (D14.11), which is
    # exactly the "rough idea" semantic wanted.
    "         SUM(GREATEST(dc.qty - COALESCE(inv.owned, 0), 0) "
    "             * COALESCE(cp.price_usd, 0)) AS missing_usd, "
    # Unpriced missing cards are COUNTED, not silently charged at 0 —
    # ~5% of the corpus has no price, and a deck missing three unpriced
    # staples would otherwise read as cheap with nothing saying so.
    "         SUM(CASE WHEN GREATEST(dc.qty - COALESCE(inv.owned, 0), 0) > 0 "
    "                   AND cp.price_usd IS NULL THEN 1 ELSE 0 END) "
    "             AS unpriced_missing, "
    # D19.47: the CURRENT unresolved count, from the same rows the
    # card table reads. `deck_versions.unresolved_count` is a
    # write-time snapshot nothing recomputes, so after a parser fix
    # it goes stale and the badge disagrees with the list beneath it.
    "         COUNT(*) FILTER (WHERE dc.oracle_id IS NULL) AS unresolved "
    "  FROM deck_cards dc LEFT JOIN inv ON inv.oracle_id = dc.oracle_id "
    # LATERAL … LIMIT 1, not a plain join: `cards` can hold several rows
    # per name (reversible faces, §3.4) and a plain join would multiply
    # the deck's rows and inflate the total.
    "  LEFT JOIN LATERAL ("
    "    SELECT price_usd FROM cards WHERE oracle_id = dc.oracle_id LIMIT 1"
    "  ) cp ON TRUE "
    "  GROUP BY dc.deck_id"
    ") "
)


async def create_deck(
    conn: _Connection,
    *,
    name: str,
    decklist: str,
    source: str,
    branch: str = "main",
    message: str | None = None,
    deck_format: str = "commander",
    commander: str | None = None,
    companion: str | None = None,
    notes: str | None = None,
    deckbox_url: str | None = None,
    moxfield_url: str | None = None,
    archidekt_url: str | None = None,
    folder: str | None = None,
    filename: str | None = None,
    raw_content: bytes | None = None,
) -> dict[str, Any]:
    """Create a deck and its version 1 in one transaction."""
    res = await conn.execute(
        text(
            "INSERT INTO decks "  # noqa: S608
            "(name, format, commander, companion, notes, deckbox_url, "
            " moxfield_url, archidekt_url, folder) "
            "VALUES (:name, :format, :commander, :companion, :notes, "
            " :deckbox_url, :moxfield_url, :archidekt_url, :folder) "
            f"RETURNING {_DECK_RETURNING}"
        ),
        {
            "name": name,
            "format": deck_format,
            "commander": commander,
            "companion": companion,
            "notes": notes,
            "deckbox_url": deckbox_url,
            "moxfield_url": moxfield_url,
            "archidekt_url": archidekt_url,
            "folder": folder,
        },
    )
    deck = res.mappings().one()
    result = await append_version(
        conn,
        str(deck["id"]),
        decklist,
        source=source,
        branch=branch,
        message=message,
        filename=filename,
        raw_content=raw_content,
    )
    # A brand-new deck can never be pinned, so head always follows this
    # write — but keep the same override shape `update_deck` uses, for one
    # `_deck_payload` contract shared by both write paths.
    deck = {**deck, "head_version": result.head_version, "head_pinned": result.head_pinned}
    return _deck_payload(
        deck,
        result.version,
        version_created=result.created,
        diff_bases=result.diff_bases,
        imported_version=result.version["version"],
    )


async def fork_deck(
    conn: _Connection, deck_id: str, *, name: str | None
) -> dict[str, Any] | None:
    """Fork a deck: a NEW deck seeded at version 1 from the SOURCE's HEAD.

    D19.41. A `budget` or `pre-rotation` build is played independently and
    never reconverges with the original — exactly what a branch (D19.33)
    is wrong for and a separate deck is right for. This is a THIN decision
    layer over `create_deck`, which itself calls `append_version` — no
    second insert path, so per-branch dedup, head auto-follow, and the
    `deck_cards` rebuild are all inherited unchanged.

    Seeds from HEAD (`_head_version_row`), never merely the newest version
    — a deck whose head is pinned to an older version must fork THAT
    content, not whatever was imported most recently onto some branch it
    is not currently on.

    History is NOT copied: only `create_deck`'s own version 1 is written.
    Carrying the source's past would make the two decks' version numbers
    describe different timelines while looking like the same one.

    Service URLs (`deckbox_url` / `moxfield_url` / `archidekt_url`) and
    `notes` are deliberately NOT inherited — the fork is not the source's
    remote deck, and copying a link would point a second local deck at the
    same remote, so a later sync could overwrite the wrong list. Left off
    the `create_deck` call entirely rather than passed as None, so this
    reads the same way at the call site as at every other omitted field.

    `trunk_branch` is NOT inherited either: a fresh fork has only ever
    written branch `main`, so the source's multi-branch head-pinning
    configuration has nothing to apply to yet. `create_deck` never sets it
    (not in its parameter list), so the new deck gets `NULL` — "follow
    every import" — same as any other brand-new deck.

    `source = 'fork'` on the seeded version — none of `upload` / `paste` /
    `url` / `edit` / `restore` are honest about "this version 1 is really
    another deck's HEAD, copied": it is not a restore, because nothing is
    being reapplied onto the SAME deck's own timeline.

    Returns None when `deck_id` is unknown or the source deck somehow has
    no HEAD content to seed from (unreachable in practice — see
    `_head_version_row`'s docstring).
    """
    source_deck = (
        await conn.execute(
            text(f"SELECT {_DECK_RETURNING} FROM decks WHERE id = CAST(:id AS uuid)"),  # noqa: S608
            {"id": deck_id},
        )
    ).mappings().one_or_none()
    if source_deck is None:
        return None

    head_ver = await _head_version_row(conn, deck_id, source_deck["head_version"])
    if head_ver is None:
        return None

    fork_name = (name.strip() if name and name.strip() else f"{source_deck['name']} (fork)")
    return await create_deck(
        conn,
        name=fork_name[:MAX_NAME_LEN],
        decklist=head_ver["decklist"] or "",
        source="fork",
        deck_format=source_deck["format"],
        commander=source_deck["commander"],
        companion=source_deck["companion"],
        folder=source_deck["folder"],
    )


async def get_deck(conn: _Connection, deck_id: str) -> dict[str, Any] | None:
    """One deck plus its HEAD content, or None when the id is unknown."""
    res = await conn.execute(
        text(f"SELECT {_DECK_RETURNING} FROM decks WHERE id = CAST(:id AS uuid)"),  # noqa: S608
        {"id": deck_id},
    )
    deck = res.mappings().one_or_none()
    if deck is None:
        return None
    version = await _head_version_row(conn, deck_id, deck["head_version"])
    # One scalar read rather than a second definition of "unresolved":
    # the same `oracle_id IS NULL` rule the card list and the library
    # badge use, so all three cannot disagree.
    unresolved_now = (
        await conn.execute(
            text(
                "SELECT COUNT(*) AS n FROM deck_cards "
                "WHERE deck_id = CAST(:id AS uuid) AND oracle_id IS NULL"
            ),
            {"id": deck_id},
        )
    ).mappings().one()["n"]
    return _deck_payload(deck, version, unresolved_now=int(unresolved_now))


async def list_decks(
    conn: _Connection,
    q: str | None = None,
    page: int = 1,
    limit: int = 50,
    folder: str | None = None,
    include_subfolders: bool = False,
    unfiled: bool = False,
    sort: str = "updated",
    direction: str | None = None,
    completion: str | None = None,
) -> dict[str, Any]:
    """Newest-touched-first library listing.

    The LATERAL reads each deck's HEAD `decklist` (D19.34) so the effective
    commander can be derived, but the RESPONSE omits it — browsing a
    library must not ship every deck's full body over the wire.
    """
    offset = (max(1, page) - 1) * limit
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    clauses: list[str] = []
    if q and q.strip():
        clauses.append("d.name ILIKE :q")
        params["q"] = f"%{q.strip()}%"
    if unfiled:
        clauses.append("d.folder IS NULL")
    elif folder:
        params["folder"] = folder
        if include_subfolders:
            # ESCAPE is not optional — `%`/`_` are legal in a folder name.
            params["folder_prefix"] = folder_like_prefix(folder)
            clauses.append(
                "(d.folder = :folder "
                "OR d.folder LIKE :folder_prefix ESCAPE '\\')"
            )
        else:
            clauses.append("d.folder = :folder")
    # D19.44. Rides D19.21's coverage CTE instead of counting ownership a
    # second time, so the filter and the `coverage_pct` on each row cannot
    # disagree. A whitelist, never interpolation — the value arrives as text.
    #
    # `cov.total > 0` is load-bearing on the complete side: a deck the
    # derived index has not covered yet has NULL coverage, and "not indexed"
    # is not "you own all of it" (the same distinction D19.21 draws by
    # reporting `coverage_pct` as None rather than 0). Such a deck matches
    # NEITHER filter, which is the honest answer.
    if completion is not None:
        predicate = _COMPLETION_PREDICATES.get(completion)
        if predicate is None:
            raise ValueError(f"unknown completion {completion!r}")
        clauses.append(predicate)
    where = f"WHERE {' AND '.join(clauses)} " if clauses else ""

    expression = _SORT_EXPRESSIONS.get(sort)
    if expression is None:
        raise ValueError(f"unknown sort {sort!r}")
    resolved = (direction or _DEFAULT_DIRECTION[sort]).upper()
    if resolved not in ("ASC", "DESC"):
        raise ValueError(f"unknown direction {direction!r}")
    # NULLS LAST regardless of direction: "no coverage known" is never the
    # best answer to "which deck is closest to built?".
    order_by = f"{expression} {resolved} NULLS LAST"

    # The COUNT has its OWN from-clause while sharing `where` with the row
    # query below — so a coverage predicate has to drag the CTE and the join
    # along or it references an alias that is not in scope. Pulled in only
    # when the filter is active: an unfiltered listing keeps the cheap count
    # it has always had, rather than paying for an aggregate nobody asked
    # for. Pinned both ways.
    count_prefix = _COVERAGE_CTE if completion is not None else ""
    count_join = "LEFT JOIN cov ON cov.deck_id = d.id " if completion is not None else ""
    total = int(
        (
            await conn.execute(
                text(
                    f"{count_prefix}"  # noqa: S608
                    f"SELECT COUNT(*) AS total FROM decks d {count_join}{where}"
                ),
                params,
            )
        )
        .mappings()
        .one()["total"]
    )

    rows = (
        (
            await conn.execute(
                text(
                    f"{_COVERAGE_CTE}"  # noqa: S608
                    f"SELECT {', '.join(f'd.{c}' for c in _DECK_COLUMNS)}, "
                    "       v.version, v.decklist, v.total_cards, v.distinct_cards, "
                    "       v.unresolved_count, v.created_at AS version_created_at, "
                    "       cov.total AS cov_total, cov.owned AS cov_owned, "
                    "       cov.missing_usd AS cov_missing_usd, "
                    "       cov.unpriced_missing AS cov_unpriced_missing, "
                    "       cov.unresolved AS cov_unresolved "
                    "FROM decks d "
                    "LEFT JOIN cov ON cov.deck_id = d.id "
                    # HEAD-AWARE "latest" definition
                    "LEFT JOIN LATERAL ("
                    "  SELECT version, decklist, total_cards, distinct_cards, "
                    "         unresolved_count, created_at "
                    "  FROM deck_versions WHERE deck_id = d.id "
                    "  AND version = COALESCE(d.head_version, "
                    "    (SELECT MAX(version) FROM deck_versions WHERE deck_id = d.id)"
                    "  )"
                    ") v ON TRUE "
                    f"{where}"
                    f"ORDER BY {order_by} "
                    "LIMIT :limit OFFSET :offset"
                ),
                params,
            )
        )
        .mappings()
        .all()
    )

    data = [
        {
            "object": "deck",
            "id": str(r["id"]),
            "name": r["name"],
            "format": r["format"],
            "commander": effective_commander(r["commander"], r["decklist"]),
            "companion": effective_companion(r["companion"], r["decklist"]),
            "notes": r["notes"],
            "deckbox_url": r["deckbox_url"],
            "moxfield_url": r["moxfield_url"],
            "archidekt_url": r["archidekt_url"],
            "folder": r["folder"],
            "version": r["version"],
            "total_cards": r["total_cards"] or 0,
            "distinct_cards": r["distinct_cards"] or 0,
            # D19.47: derived from `deck_cards` so the badge and the
            # card list beneath it read the same rows. A deck with no
            # indexed rows has no opinion from the index, so it falls
            # back to the stored write-time value — the only evidence
            # there is, and `coverage_pct` is already null in that case.
            "unresolved_count": (
                int(r["cov_unresolved"])
                if r["cov_total"] is not None
                else (r["unresolved_count"] or 0)
            ),
            "owned_cards": int(r["cov_owned"] or 0),
            # None, not 0: "not indexed yet" is not "you own none of it".
            "coverage_pct": (
                round(int(r["cov_owned"] or 0) / int(r["cov_total"]) * 100, 2)
                if r["cov_total"]
                else None
            ),
            "missing_usd": (
                round(float(r["cov_missing_usd"]), 2)
                if r["cov_missing_usd"] is not None
                else None
            ),
            "unpriced_missing": int(r["cov_unpriced_missing"] or 0),
            "created_at": _iso(r["created_at"]),
            "updated_at": _iso(r["updated_at"]),
        }
        for r in rows
    ]

    return {
        "object": "list",
        "data": data,
        "total_decks": total,
        "page": page,
        "limit": limit,
        "has_more": (offset + len(data)) < total,
    }


async def update_deck(
    conn: _Connection,
    deck_id: str,
    *,
    fields: dict[str, Any] | None = None,
    decklist: str | None = None,
    source: str = "edit",
    branch: str = "main",
    message: str | None = None,
    filename: str | None = None,
    raw_content: bytes | None = None,
) -> dict[str, Any] | None:
    """Patch metadata and/or append a new version. None when id is unknown."""
    exists = (
        (
            await conn.execute(
                text("SELECT id FROM decks WHERE id = CAST(:id AS uuid)"),
                {"id": deck_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if exists is None:
        return None

    supplied = {k: v for k, v in (fields or {}).items() if k in MUTABLE_FIELDS}
    assignments = ", ".join(f"{col} = :{col}" for col in supplied)
    # `updated_at` moves on every PATCH, including a metadata-only one, so
    # the library listing's ordering reflects real activity.
    set_clause = f"{assignments}, updated_at = now()" if assignments else "updated_at = now()"
    deck = (
        (
            await conn.execute(
                text(
                    f"UPDATE decks SET {set_clause} "  # noqa: S608
                    f"WHERE id = CAST(:id AS uuid) RETURNING {_DECK_RETURNING}"
                ),
                {**supplied, "id": deck_id},
            )
        )
        .mappings()
        .one()
    )

    if decklist is not None:
        result = await append_version(
            conn,
            deck_id,
            decklist,
            source=source,
            branch=branch,
            message=message,
            filename=filename,
            raw_content=raw_content,
        )
        # The `deck` row above was fetched BEFORE the append, so its
        # `head_version`/`head_pinned` may already be stale — a non-pinned
        # write just moved head. Overlay the post-write values rather than
        # re-querying: `AppendResult` already carries them.
        deck = {
            **deck,
            "head_version": result.head_version,
            "head_pinned": result.head_pinned,
        }
        # `version`/`decklist`/`commander` must describe HEAD, never merely
        # the version just written (D19.34's trap) — those coincide unless
        # the deck is pinned somewhere else, in which case a write can land
        # without moving head at all.
        if result.version["version"] == result.head_version:
            head_row = result.version
        else:
            head_row = await _head_version_row(conn, deck_id, result.head_version)
        return _deck_payload(
            deck,
            head_row,
            version_created=result.created,
            diff_bases=result.diff_bases,
            imported_version=result.version["version"],
        )
    version = await _head_version_row(conn, deck_id, deck["head_version"])
    return _deck_payload(deck, version)


async def delete_deck(conn: _Connection, deck_id: str) -> bool:
    """Delete a deck. Versions go with it via ON DELETE CASCADE."""
    res = await conn.execute(
        text("DELETE FROM decks WHERE id = CAST(:id AS uuid)"), {"id": deck_id}
    )
    return bool(getattr(res, "rowcount", 0))


async def deck_exists(conn: _Connection, deck_id: str) -> bool:
    """True when the id is a real deck. Used where a branch-filtered
    version listing can legitimately come back empty for a real deck, so
    "no rows" cannot be read as "no deck" the way `list_versions` normally
    can (D19.34)."""
    row = (
        await conn.execute(
            text("SELECT 1 FROM decks WHERE id = CAST(:id AS uuid)"), {"id": deck_id}
        )
    ).mappings().one_or_none()
    return row is not None


async def list_versions(
    conn: _Connection, deck_id: str, limit: int = 100, branch: str | None = None
) -> list[dict[str, Any]]:
    """Newest-first version history for one deck, bodies excluded.

    `branch` optionally filters to one branch's own history — the same
    global-version-number list, narrowed.
    """
    params: dict[str, Any] = {"deck_id": deck_id, "limit": limit}
    branch_clause = ""
    if branch:
        branch_clause = "AND branch = :branch "
        params["branch"] = branch
    rows = (
        (
            await conn.execute(
                text(
                    f"SELECT {_VERSION_SUMMARY} FROM deck_versions "  # noqa: S608
                    "WHERE deck_id = CAST(:deck_id AS uuid) "
                    f"{branch_clause}"
                    "ORDER BY version DESC LIMIT :limit"
                ),
                params,
            )
        )
        .mappings()
        .all()
    )
    return [_version_payload(r) for r in rows]


async def get_version(
    conn: _Connection, deck_id: str, version: int
) -> dict[str, Any] | None:
    """One version including its canonical decklist text."""
    row = (
        (
            await conn.execute(
                text(
                    f"SELECT {_VERSION_RETURNING} FROM deck_versions "  # noqa: S608
                    "WHERE deck_id = CAST(:deck_id AS uuid) AND version = :version"
                ),
                {"deck_id": deck_id, "version": version},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    return {**_version_payload(row), "decklist": row["decklist"]}


async def get_version_blob(
    conn: _Connection, deck_id: str, version: int
) -> dict[str, Any] | None:
    """The bytes to hand back for a download, plus the text fallback.

    `raw_content` is NULL for paste / edit / restore versions, which never
    had a file; the caller synthesises a `.txt` from `decklist` in that case.
    """
    row = (
        (
            await conn.execute(
                text(
                    "SELECT version, filename, raw_content, decklist "
                    "FROM deck_versions "
                    "WHERE deck_id = CAST(:deck_id AS uuid) AND version = :version"
                ),
                {"deck_id": deck_id, "version": version},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    raw = row["raw_content"]
    return {
        "version": row["version"],
        "filename": row["filename"],
        "raw_content": bytes(raw) if raw is not None else None,
        "decklist": row["decklist"],
    }


async def restore_version(
    conn: _Connection, deck_id: str, version: int
) -> AppendResult | None:
    """Re-apply an old version by APPENDING it as a new one.

    Never truncates back to `version` — history is append-only, so the
    restore itself becomes a visible entry. Restoring the version that is
    already current is caught by the ordinary dedup in `append_version`
    and returns `created=False`.

    Appends onto the SAME branch as the version being restored — a restore
    is predictable regardless of which branch happens to be in view, and
    restoring a `deckbox` version must not silently land it on `main`.
    """
    row = (
        (
            await conn.execute(
                text(
                    "SELECT decklist, branch FROM deck_versions "
                    "WHERE deck_id = CAST(:deck_id AS uuid) AND version = :version"
                ),
                {"deck_id": deck_id, "version": version},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    return await append_version(
        conn, deck_id, row["decklist"], source="restore", branch=row["branch"]
    )


# ---------------------------------------------------------------------------
# D19.45 — completion plan. "What do I buy next to finish whole decks?"
#
# The objective is DECKS COMPLETED within a budget, not cards-per-dollar. A
# leverage ranking was considered and rejected: a card five decks want that
# finishes none of them buys nothing playable, while a card that completes
# one deck does. Author's rule, and it makes this a set-cover shape rather
# than a sort — so the answer is a greedy PLAN, not an ORDER BY.
# ---------------------------------------------------------------------------

_SHORTFALL_SQL = (
    "WITH inv AS ("
    "  SELECT oracle_id, SUM(quantity) AS owned FROM inventory GROUP BY oracle_id"
    ") "
    "SELECT dc.deck_id, d.name AS deck_name, dc.oracle_id, "
    "       COALESCE(c.name, dc.card_name) AS card_name, "
    "       GREATEST(dc.qty - COALESCE(inv.owned, 0), 0) AS missing, "
    "       c.price_usd "
    "FROM deck_cards dc "
    "JOIN decks d ON d.id = dc.deck_id "
    "LEFT JOIN inv ON inv.oracle_id = dc.oracle_id "
    "LEFT JOIN cards c ON c.oracle_id = dc.oracle_id "
    # An unresolved row (oracle_id NULL) can never be matched to inventory
    # and has no price, so it cannot be planned for. It is reported through
    # the deck's `unresolved` flag instead of silently making the deck look
    # cheap — the same posture `missing_usd` takes for unpriced cards.
    "WHERE dc.oracle_id IS NOT NULL "
    "  AND GREATEST(dc.qty - COALESCE(inv.owned, 0), 0) > 0"
)


def _greedy_plan(
    gaps: dict[str, dict[str, int]],
    names: dict[str, str],
    prices: dict[str, float | None],
    card_names: dict[str, str],
    *,
    by_cards: bool,
    budget: float | None,
    max_cards: int | None,
    unpriced_decks: set[str],
) -> dict[str, Any]:
    """One greedy pass. `by_cards` swaps the objective, not the machinery.

    Cost and card count are both MARGINAL — measured against a cart of what
    earlier steps already bought — because buying for one deck can finish
    another for nothing. Summing per-deck `missing_usd` instead would have
    reported $14.54 where the real spend is $9.27 on live data.

    The cart is quantity-aware and SHARED across decks, matching how
    coverage already treats inventory: one bought copy satisfies every deck
    that wants one. That is why a duplicated deck completes at $0.00.
    """
    cart: dict[str, int] = {}
    spent, bought_cards = 0.0, 0
    done: set[str] = set()
    steps: list[dict[str, Any]] = []

    while True:
        best: tuple[tuple[float, float, str], str, dict[str, int], float, int] | None = None
        for deck_id, want in gaps.items():
            if deck_id in done:
                continue
            # A deck with an unpriced gap has no trustworthy cost, so the
            # cost objective cannot rank it. The card objective can, which
            # is the whole reason both plans exist.
            if deck_id in unpriced_decks and not by_cards:
                continue
            short = {
                oid: n
                for oid, need in want.items()
                if (n := need - cart.get(oid, 0)) > 0
            }
            cost = sum(n * (prices.get(oid) or 0.0) for oid, n in short.items())
            count = sum(short.values())
            if budget is not None and spent + cost > budget + 1e-9:
                continue
            if max_cards is not None and bought_cards + count > max_cards:
                continue
            # Ties need a deterministic second AND third key or the plan
            # wobbles between identical requests, which reads as a bug.
            key = (
                (float(count), cost, deck_id) if by_cards else (cost, float(count), deck_id)
            )
            if best is None or key < best[0]:
                best = (key, deck_id, short, cost, count)
        if best is None:
            break

        _, deck_id, short, cost, count = best
        for oid, n in short.items():
            cart[oid] = cart.get(oid, 0) + n
        spent += cost
        bought_cards += count
        done.add(deck_id)
        steps.append(
            {
                "deck_id": deck_id,
                "deck_name": names[deck_id],
                "marginal_usd": round(cost, 2),
                "marginal_cards": count,
                "running_usd": round(spent, 2),
                "running_cards": bought_cards,
                "cost_is_partial": deck_id in unpriced_decks,
                "cards": sorted(
                    (
                        {
                            "oracle_id": oid,
                            "name": card_names[oid],
                            "qty": n,
                            "price_usd": prices.get(oid),
                        }
                        for oid, n in short.items()
                    ),
                    key=lambda c: (-(c["price_usd"] or 0.0), c["name"]),
                ),
            }
        )

    return {
        "decks_completed": len(steps),
        "spend_usd": round(spent, 2),
        "cards_to_buy": bought_cards,
        # `by_cards` may include decks whose gap has an unpriced card, so its
        # total is a FLOOR, never a promise. Presenting a partial sum as
        # exact is the trap `missing_usd` already sets; naming it here is
        # what keeps this plan honest.
        "spend_usd_is_floor": any(s["cost_is_partial"] for s in steps),
        "steps": steps,
    }


async def completion_plan(
    conn: _Connection,
    *,
    budget: float | None = None,
    max_cards: int | None = None,
) -> dict[str, Any]:
    """Two greedy plans over the same shortfall matrix — one query, no schema.

    `by_cost` minimises spend; `by_cards` minimises how many cards you buy.
    Neither is the default: on real data at `budget=10` they complete the
    same 3 decks while buying 20 cards vs 5, and returning a single ranking
    would hide that the choice exists.
    """
    rows = (await conn.execute(text(_SHORTFALL_SQL))).mappings().all()

    gaps: dict[str, dict[str, int]] = {}
    names: dict[str, str] = {}
    prices: dict[str, float | None] = {}
    card_names: dict[str, str] = {}
    for r in rows:
        deck_id = str(r["deck_id"])
        oid = str(r["oracle_id"])
        gaps.setdefault(deck_id, {})[oid] = int(r["missing"])
        names[deck_id] = r["deck_name"]
        prices[oid] = float(r["price_usd"]) if r["price_usd"] is not None else None
        card_names[oid] = r["card_name"]

    unpriced_decks = {
        d for d, want in gaps.items() if any(prices.get(o) is None for o in want)
    }

    def build(by_cards: bool) -> dict[str, Any]:
        return _greedy_plan(
            gaps, names, prices, card_names,
            by_cards=by_cards, budget=budget, max_cards=max_cards,
            unpriced_decks=unpriced_decks,
        )

    by_cost = build(False)
    by_cost["skipped_unpriced"] = sorted(
        (
            {
                "deck_id": d,
                "deck_name": names[d],
                "unpriced_cards": sorted(
                    card_names[o] for o in gaps[d] if prices.get(o) is None
                ),
            }
            for d in unpriced_decks
        ),
        key=lambda s: s["deck_name"],
    )
    return {
        "object": "deck_completion_plan",
        "budget_usd": budget,
        "max_cards": max_cards,
        "incomplete_decks": len(gaps),
        "by_cost": by_cost,
        "by_cards": build(True),
        # Said out loud rather than left in the docs: budgeted set cover with
        # overlapping sets is NP-hard, and greedy left $0.73 of $10 unspent
        # on the live library. A caller told the answer is optimal will stop
        # looking; a caller told it is greedy can spend the remainder.
        "note": (
            "Greedy, not proven optimal — a different combination may complete "
            "more decks for the same budget."
        ),
    }
