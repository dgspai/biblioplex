"""D19.4 — stored-deck persistence service."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from biblioplex.decklist.resolve import ResolveResult
from biblioplex.decks import service as deck_service


def _result(rows: list[dict[str, Any]] | None = None, rowcount: int = 0) -> MagicMock:
    """Fake SQLAlchemy result exposing `.mappings().one()/.one_or_none()/.all()`."""
    rows = rows or []
    mappings = MagicMock()
    mappings.all.return_value = rows
    mappings.one.return_value = rows[0] if rows else None
    mappings.one_or_none.return_value = rows[0] if rows else None
    res = MagicMock()
    res.mappings.return_value = mappings
    res.rowcount = rowcount
    return res


def _conn(*results: MagicMock) -> AsyncMock:
    conn = AsyncMock()
    it = iter(results)

    def _next(*_a: Any, **_k: Any) -> Any:
        try:
            return next(it)
        except StopIteration:
            return _result()

    conn.execute.side_effect = _next
    return conn


def _sql(conn: AsyncMock, index: int) -> str:
    return str(conn.execute.await_args_list[index].args[0])


DECK_ROW = {
    "id": "11111111-1111-1111-1111-111111111111",
    "name": "Iroh gift",
    "format": "commander",
    "commander": None,
    "companion": None,
    "notes": None,
    "deckbox_url": None,
    "moxfield_url": None,
    "archidekt_url": None,
    "folder": None,
    "head_version": None,
    "head_pinned": False,
    "trunk_branch": None,
    "created_at": MagicMock(isoformat=lambda: "2026-08-18T00:00:00+00:00"),
    "updated_at": MagicMock(isoformat=lambda: "2026-08-18T00:00:00+00:00"),
}


def _version_row(
    version: int = 1, decklist: str = "1 Sol Ring\n", branch: str = "main", message: str | None = None
) -> dict[str, Any]:
    return {
        "id": "22222222-2222-2222-2222-222222222222",
        "version": version,
        "branch": branch,
        "message": message,
        "decklist": decklist,
        "source": "paste",
        "filename": None,
        "size_bytes": len(decklist.encode()),
        "total_cards": 1,
        "distinct_cards": 1,
        "unresolved_count": 0,
        "created_at": MagicMock(isoformat=lambda: "2026-08-18T00:00:00+00:00"),
    }


def _head_row(
    head_version: int | None = None,
    head_pinned: bool = False,
    trunk_branch: str | None = None,
) -> MagicMock:
    """The `SELECT head_version, head_pinned, trunk_branch FROM decks`
    `append_version` runs first."""
    return _result([
        {"head_version": head_version, "head_pinned": head_pinned, "trunk_branch": trunk_branch}
    ])


def _max_version_row(max_version: int | None) -> MagicMock:
    return _result([{"max_version": max_version}])


@pytest.fixture(autouse=True)
def _no_resolve() -> Any:
    """Resolution is exercised in its own suite; stub it out here."""
    with patch.object(
        deck_service, "resolve_names", new=AsyncMock(return_value=ResolveResult())
    ) as m:
        yield m


# ---- counting ----


def test_counts_span_every_section() -> None:
    total, distinct, names = deck_service.decklist_counts(
        "Commander\n1 Hope of Ghirapur\n\nDeck\n38 Forest\n1 Sol Ring\n"
        "\nSideboard\n4 Grapeshot\n"
    )
    assert total == 44
    assert distinct == 4
    assert set(names) == {"Hope of Ghirapur", "Forest", "Sol Ring", "Grapeshot"}


def test_counts_do_not_double_count_commander_or_companion() -> None:
    """D19.10 regression. `parse_sections` puts commander and companion in
    `main` as well as their own bucket, so `decklist_counts` must span
    `main + sideboard` only. Summing all four buckets — which is what the
    pre-D19.10 code did — reports 47 here instead of 45."""
    total, distinct, names = deck_service.decklist_counts(
        "Commander\n1 Hope of Ghirapur\n"
        "Companion\n1 Lurrus of the Dream-Den\n"
        "Deck\n38 Forest\n1 Sol Ring\n"
        "Sideboard\n4 Grapeshot\n"
    )
    assert total == 45
    assert distinct == 5
    assert set(names) == {
        "Hope of Ghirapur", "Lurrus of the Dream-Den",
        "Forest", "Sol Ring", "Grapeshot",
    }


def test_counts_deduplicate_by_case_folded_name() -> None:
    total, distinct, _ = deck_service.decklist_counts("1 Sol Ring\n1 SOL RING\n")
    assert (total, distinct) == (2, 1)


# ---- effective commander ----


def test_effective_commander_prefers_the_override() -> None:
    assert (
        deck_service.effective_commander("Krenko", "Commander\n1 Hope of Ghirapur\n")
        == "Krenko"
    )


def test_effective_commander_falls_back_to_the_parsed_section() -> None:
    assert (
        deck_service.effective_commander(None, "Commander\n1 Hope of Ghirapur\n")
        == "Hope of Ghirapur"
    )


def test_effective_commander_joins_partners() -> None:
    parsed = deck_service.effective_commander(
        None, "Commander\n1 Thrasios, Triton Hero\n1 Tymna the Weaver\n"
    )
    assert parsed == "Thrasios, Triton Hero // Tymna the Weaver"


def test_effective_commander_is_none_when_no_section_exists() -> None:
    """Every Deckbox export lands here — that is why the override column exists."""
    assert deck_service.effective_commander(None, "1 Sol Ring\n") is None


# ---- version append (D19.34: head fetch, per-branch dedup, head move) ----

_DECK_ID = "11111111-1111-1111-1111-111111111111"


def _new_version_conn(
    *,
    branch: str = "main",
    max_version: int | None = None,
    head_version: int | None = None,
    head_pinned: bool = False,
    trunk_branch: str | None = None,
    branch_latest: dict[str, Any] | None = None,
    main_latest: dict[str, Any] | None = None,
    inserted: dict[str, Any],
) -> AsyncMock:
    """A conn primed for a NOT-deduped `append_version` call: head fetch,
    branch dedup lookup (empty or non-matching), diff_bases' extra lookup
    of `main`'s own latest ONLY when `branch != "main"` (mirrors
    `_diff_bases`'s query reuse), next-version-number lookup, then the
    INSERT. The UPDATE head + rebuild DELETE/INSERT that follow don't
    inspect their results, so the fallback empty `_result()` covers them.
    """
    stubs = [
        _head_row(head_version, head_pinned, trunk_branch),
        _result([branch_latest] if branch_latest else []),
    ]
    if branch != "main":
        stubs.append(_result([main_latest] if main_latest else []))
    stubs += [_max_version_row(max_version), _result([inserted])]
    return _conn(*stubs)


@pytest.mark.asyncio
async def test_first_version_is_numbered_one() -> None:
    conn = _new_version_conn(inserted=_version_row(1))
    result = await deck_service.append_version(
        conn, _DECK_ID, "1 Sol Ring\n", source="paste"
    )
    assert result.created is True
    assert result.version["version"] == 1
    assert result.head_version == 1
    assert result.head_pinned is False


@pytest.mark.asyncio
async def test_identical_decklist_is_a_noop_returning_the_existing_version() -> None:
    """Re-uploading the same export must not junk up the timeline."""
    existing = _version_row(3, "1 Sol Ring\n")
    conn = _conn(_head_row(3, False), _result([existing]))
    result = await deck_service.append_version(
        conn, _DECK_ID, "1 Sol Ring\n", source="upload"
    )
    assert result.created is False
    assert result.version["version"] == 3
    # Head fetch + the branch's own dedup lookup — no INSERT, no head move.
    assert conn.execute.await_count == 2


@pytest.mark.asyncio
async def test_dedup_on_a_different_branch_needs_a_second_lookup_for_the_main_base() -> None:
    """Dedup on a non-`main` branch still needs `main`'s own latest for
    `diff_bases` — `main`'s latest is a DIFFERENT row than the branch being
    deduped, so it cannot be inferred from the branch lookup alone."""
    existing = _version_row(3, "1 Sol Ring\n", branch="deckbox")
    main_latest = _version_row(2, "1 Sol Ring\n1 Mana Crypt\n", branch="main")
    conn = _conn(_head_row(3, False), _result([existing]), _result([main_latest]))
    result = await deck_service.append_version(
        conn, _DECK_ID, "1 Sol Ring\n", source="upload", branch="deckbox"
    )
    assert result.created is False
    assert result.diff_bases == {"main": 2, "branch": 3}
    assert conn.execute.await_count == 3


@pytest.mark.asyncio
async def test_changed_decklist_appends_the_next_version() -> None:
    conn = _new_version_conn(
        max_version=3,
        branch_latest=_version_row(3, "1 Sol Ring\n"),
        inserted=_version_row(4),
    )
    result = await deck_service.append_version(
        conn, _DECK_ID, "2 Sol Ring\n", source="edit"
    )
    assert result.created is True
    params = conn.execute.await_args_list[3].args[1]
    assert params["version"] == 4
    assert params["source"] == "edit"
    assert params["branch"] == "main"


@pytest.mark.asyncio
async def test_version_insert_uses_cast_not_double_colon() -> None:
    """Bug.27 — `text()` treats `::` as an escaped colon and skips the bind."""
    conn = _new_version_conn(inserted=_version_row(1))
    await deck_service.append_version(conn, _DECK_ID, "1 Sol Ring\n", source="paste")
    for i in range(conn.execute.await_count):
        assert "::" not in _sql(conn, i)
        assert "CAST(" in _sql(conn, i)


@pytest.mark.asyncio
async def test_branch_dedup_lookup_is_ordered_and_limited() -> None:
    """The per-branch dedup lookup — NOT head-aware, deliberately."""
    conn = _new_version_conn(inserted=_version_row(1))
    await deck_service.append_version(conn, _DECK_ID, "1 Sol Ring\n", source="paste")
    sql = _sql(conn, 1)
    assert "ORDER BY version DESC" in sql
    assert "LIMIT 1" in sql
    assert "branch = :branch" in sql


@pytest.mark.asyncio
async def test_raw_content_defaults_to_null_and_size_tracks_the_text() -> None:
    conn = _new_version_conn(inserted=_version_row(1))
    await deck_service.append_version(conn, _DECK_ID, "1 Sol Ring\n", source="paste")
    params = conn.execute.await_args_list[3].args[1]
    assert params["raw_content"] is None
    assert params["size_bytes"] == len(b"1 Sol Ring\n")


@pytest.mark.asyncio
async def test_upload_keeps_the_original_bytes_and_sizes_by_them() -> None:
    raw = b"Count,Name\n1,Sol Ring\n"
    conn = _new_version_conn(inserted=_version_row(1))
    await deck_service.append_version(
        conn,
        _DECK_ID,
        "1 Sol Ring\n",
        source="upload",
        filename="deck.csv",
        raw_content=raw,
    )
    params = conn.execute.await_args_list[3].args[1]
    assert params["raw_content"] == raw
    assert params["size_bytes"] == len(raw)
    assert params["filename"] == "deck.csv"


@pytest.mark.asyncio
async def test_unresolved_count_is_recorded(_no_resolve: AsyncMock) -> None:
    _no_resolve.return_value = ResolveResult(unresolved=["Lighning Bolt", "Sol Rng"])
    conn = _new_version_conn(inserted=_version_row(1))
    await deck_service.append_version(conn, _DECK_ID, "1 Sol Ring\n", source="paste")
    assert conn.execute.await_args_list[3].args[1]["unresolved_count"] == 2


@pytest.mark.asyncio
async def test_unknown_branch_is_rejected_before_any_query() -> None:
    conn = _conn()
    with pytest.raises(ValueError):
        await deck_service.append_version(
            conn, _DECK_ID, "1 Sol Ring\n", source="paste", branch="nope"
        )
    conn.execute.assert_not_called()


# ---- D19.34: per-branch dedup, head auto-follow, the pinned trap ----


@pytest.mark.asyncio
async def test_unpinned_import_moves_head_and_rebuilds_deck_cards() -> None:
    conn = _new_version_conn(
        head_version=1,
        max_version=1,
        inserted=_version_row(2, "1 Sol Ring\n1 Mana Crypt\n"),
    )
    result = await deck_service.append_version(
        conn, _DECK_ID, "1 Sol Ring\n1 Mana Crypt\n", source="edit"
    )
    assert result.head_version == 2
    assert result.head_pinned is False
    # head fetch, dedup lookup, max version, INSERT version, UPDATE head,
    # DELETE deck_cards, INSERT deck_cards.
    assert conn.execute.await_count == 7
    assert "UPDATE decks SET head_version" in _sql(conn, 4)
    assert "DELETE FROM deck_cards" in _sql(conn, 5)


@pytest.mark.asyncio
async def test_pinned_import_on_a_non_head_branch_does_not_move_head_or_rebuild() -> None:
    """The whole risk of D19.34: a write to a non-head branch of a PINNED
    deck must not touch `head_version` or `deck_cards`."""
    conn = _new_version_conn(
        branch="deckbox",
        head_version=1,
        head_pinned=True,
        branch_latest=None,
        inserted=_version_row(2, "1 Sol Ring\n", branch="deckbox"),
    )
    result = await deck_service.append_version(
        conn, _DECK_ID, "1 Sol Ring\n", source="upload", branch="deckbox"
    )
    assert result.created is True
    assert result.head_version == 1  # unchanged
    assert result.head_pinned is True
    # head fetch, dedup lookup, diff_bases' main lookup (branch != main),
    # max version, INSERT — and NOTHING ELSE: no UPDATE head, no rebuild.
    assert conn.execute.await_count == 5
    for i in range(conn.execute.await_count):
        sql = _sql(conn, i)
        assert "UPDATE decks SET head_version" not in sql
        assert "DELETE FROM deck_cards" not in sql


@pytest.mark.asyncio
async def test_trunk_branch_blocks_head_move_on_a_non_trunk_import() -> None:
    """D19.38 — `trunk_branch='main'`, import lands on `deckbox`: version
    is created, but head/deck_cards must stay exactly where the pinned
    test above leaves them — the SAME blocked path, different reason."""
    conn = _new_version_conn(
        branch="deckbox",
        head_version=1,
        head_pinned=False,
        trunk_branch="main",
        branch_latest=None,
        inserted=_version_row(2, "1 Sol Ring\n", branch="deckbox"),
    )
    result = await deck_service.append_version(
        conn, _DECK_ID, "1 Sol Ring\n", source="upload", branch="deckbox"
    )
    assert result.created is True
    assert result.head_version == 1  # unchanged
    assert result.head_pinned is False  # not pinned — just off-trunk
    assert conn.execute.await_count == 5
    for i in range(conn.execute.await_count):
        sql = _sql(conn, i)
        assert "UPDATE decks SET head_version" not in sql
        assert "DELETE FROM deck_cards" not in sql


@pytest.mark.asyncio
async def test_trunk_branch_import_moves_head() -> None:
    """A write onto the trunk branch itself follows normally."""
    conn = _new_version_conn(
        branch="main",
        head_version=1,
        max_version=1,
        trunk_branch="main",
        inserted=_version_row(2, "1 Sol Ring\n1 Mana Crypt\n"),
    )
    result = await deck_service.append_version(
        conn, _DECK_ID, "1 Sol Ring\n1 Mana Crypt\n", source="edit"
    )
    assert result.head_version == 2
    assert result.head_pinned is False
    assert "UPDATE decks SET head_version" in _sql(conn, 4)
    assert "DELETE FROM deck_cards" in _sql(conn, 5)


@pytest.mark.asyncio
async def test_null_trunk_branch_follows_every_branch() -> None:
    """Back-compat: `trunk_branch IS NULL` still auto-follows any branch."""
    conn = _new_version_conn(
        branch="moxfield",
        head_version=1,
        max_version=1,
        trunk_branch=None,
        inserted=_version_row(2, "1 Sol Ring\n", branch="moxfield"),
    )
    result = await deck_service.append_version(
        conn, _DECK_ID, "1 Sol Ring\n", source="upload", branch="moxfield"
    )
    assert result.head_version == 2
    assert result.head_pinned is False


@pytest.mark.asyncio
async def test_head_pinned_still_wins_over_trunk() -> None:
    """A pinned deck ignores `trunk_branch` entirely, even for a trunk import."""
    conn = _new_version_conn(
        branch="main",
        head_version=1,
        head_pinned=True,
        trunk_branch="main",
        branch_latest=None,
        inserted=_version_row(2, "1 Sol Ring\n"),
    )
    result = await deck_service.append_version(
        conn, _DECK_ID, "1 Sol Ring\n", source="upload", branch="main"
    )
    assert result.head_version == 1  # unchanged despite matching trunk
    assert result.head_pinned is True


def test_trunk_branch_is_a_mutable_field() -> None:
    """PATCH reaches it via the same whitelist that drives the UPDATE."""
    assert "trunk_branch" in deck_service.MUTABLE_FIELDS


@pytest.mark.asyncio
async def test_get_deck_exposes_trunk_branch() -> None:
    """The web meta panel can only render/save a value it can first read."""
    row = {**DECK_ROW, "trunk_branch": "main"}
    # Third result: D19.47's derived unresolved count. A fake conn that
    # runs dry returns None, so the fixture has to mirror the real query
    # sequence rather than the one it used to be.
    conn = _conn(_result([row]), _result([_version_row(1)]), _result([{"n": 0}]))
    deck = await deck_service.get_deck(conn, _DECK_ID)
    assert deck is not None
    assert deck["trunk_branch"] == "main"


@pytest.mark.asyncio
async def test_deduped_import_never_moves_head() -> None:
    existing = _version_row(3, "1 Sol Ring\n")
    conn = _conn(_head_row(2, False), _result([existing]))
    result = await deck_service.append_version(
        conn, _DECK_ID, "1 Sol Ring\n", source="upload"
    )
    assert result.created is False
    for i in range(conn.execute.await_count):
        assert "UPDATE decks SET head_version" not in _sql(conn, i)


@pytest.mark.asyncio
async def test_diff_bases_coincide_on_main() -> None:
    conn = _new_version_conn(
        max_version=1,
        branch_latest=_version_row(1, "1 Sol Ring\n"),
        inserted=_version_row(2, "1 Sol Ring\n1 Mana Crypt\n"),
    )
    result = await deck_service.append_version(
        conn, _DECK_ID, "1 Sol Ring\n1 Mana Crypt\n", source="edit"
    )
    assert result.diff_bases == {"main": 1, "branch": 1}


@pytest.mark.asyncio
async def test_diff_bases_are_null_on_a_decks_first_ever_version() -> None:
    conn = _new_version_conn(inserted=_version_row(1))
    result = await deck_service.append_version(
        conn, _DECK_ID, "1 Sol Ring\n", source="paste"
    )
    assert result.diff_bases == {"main": None, "branch": None}


@pytest.mark.asyncio
async def test_restore_appends_onto_the_source_versions_branch() -> None:
    source_row = {"decklist": "1 Sol Ring\n", "branch": "deckbox"}
    conn = _conn(
        _result([source_row]),  # restore's own SELECT decklist, branch
        _head_row(1, False),  # append_version's head fetch
        _result([]),  # deckbox branch has no prior version to dedup against
        _result([]),  # diff_bases' main-branch lookup (branch != main)
        _max_version_row(1),  # next version number
        _result([_version_row(2, "1 Sol Ring\n", branch="deckbox")]),  # INSERT
    )
    result = await deck_service.restore_version(conn, _DECK_ID, 1)
    assert result is not None
    assert result.version["branch"] == "deckbox"
    insert_params = conn.execute.await_args_list[5].args[1]
    assert insert_params["branch"] == "deckbox"
    assert insert_params["source"] == "restore"


# ---- CRUD ----


@pytest.mark.asyncio
async def test_create_deck_inserts_then_appends_version_one() -> None:
    conn = _conn(
        _result([DECK_ROW]),  # INSERT INTO decks
        _head_row(None, False),  # append_version's head fetch (fresh deck)
        _result([]),  # branch dedup lookup — no prior version
        _max_version_row(None),
        _result([_version_row(1)]),  # INSERT INTO deck_versions
    )
    payload = await deck_service.create_deck(
        conn, name="Iroh gift", decklist="1 Sol Ring\n", source="paste"
    )
    assert payload["object"] == "deck"
    assert payload["version"] == 1
    assert payload["version_created"] is True
    assert "INSERT INTO decks" in _sql(conn, 0)


@pytest.mark.asyncio
async def test_get_deck_returns_none_for_an_unknown_id() -> None:
    conn = _conn(_result([]))
    assert await deck_service.get_deck(conn, "11111111-1111-1111-1111-111111111111") is None


@pytest.mark.asyncio
async def test_list_decks_never_returns_the_decklist_text() -> None:
    """Browsing a library must not ship every deck's full body."""
    conn = _conn(
        _result([{"total": 1}]),
        _result([{**DECK_ROW, "version": 2, "decklist": "Commander\n1 Hope of Ghirapur\n",
                  "total_cards": 100, "distinct_cards": 99, "unresolved_count": 0,
                  "cov_total": None, "cov_owned": None,
        "cov_missing_usd": None,
        "cov_unpriced_missing": None, "cov_unresolved": 0,
                  "version_created_at": MagicMock(isoformat=lambda: "2026-08-18T00:00:00+00:00")}]),
    )
    out = await deck_service.list_decks(conn)
    assert out["object"] == "list"
    assert "decklist" not in out["data"][0]
    # ...but the commander parsed out of it still surfaces.
    assert out["data"][0]["commander"] == "Hope of Ghirapur"


@pytest.mark.asyncio
async def test_list_decks_uses_a_lateral_for_the_head_version() -> None:
    """D19.34: HEAD-aware, not merely newest (see the `head_version` trap)."""
    conn = _conn(_result([{"total": 0}]), _result([]))
    await deck_service.list_decks(conn)
    sql = _sql(conn, 1)
    assert "LATERAL" in sql
    assert "COALESCE(d.head_version" in sql


@pytest.mark.asyncio
async def test_update_deck_only_sets_the_fields_supplied() -> None:
    conn = _conn(_result([DECK_ROW]), _result([DECK_ROW]), _result([_version_row(1)]))
    await deck_service.update_deck(
        conn, "11111111-1111-1111-1111-111111111111", fields={"notes": "gift deck"}
    )
    sql = _sql(conn, 1)
    assert "notes = :notes" in sql
    assert "format" not in sql.split("SET", 1)[1].split("WHERE", 1)[0]


@pytest.mark.asyncio
async def test_update_deck_always_touches_updated_at() -> None:
    conn = _conn(_result([DECK_ROW]), _result([DECK_ROW]), _result([_version_row(1)]))
    await deck_service.update_deck(
        conn, "11111111-1111-1111-1111-111111111111", fields={"notes": "x"}
    )
    assert "updated_at = now()" in _sql(conn, 1)


@pytest.mark.asyncio
async def test_delete_deck_reports_whether_a_row_went() -> None:
    assert await deck_service.delete_deck(_conn(_result(rowcount=1)), "11111111-1111-1111-1111-111111111111") is True
    assert await deck_service.delete_deck(_conn(_result(rowcount=0)), "11111111-1111-1111-1111-111111111111") is False


# ---- D19.11 companion ----


def test_effective_companion_prefers_the_override() -> None:
    assert deck_service.effective_companion(
        "Yorion, Sky Nomad", "Companion\n1 Lurrus of the Dream-Den\n"
    ) == "Yorion, Sky Nomad"


def test_effective_companion_derives_from_the_decklist() -> None:
    assert deck_service.effective_companion(
        None, "Companion\n1 Lurrus of the Dream-Den\nDeck\n1 Sol Ring\n"
    ) == "Lurrus of the Dream-Den"


def test_effective_companion_is_none_without_a_section() -> None:
    """Every reference export lacks a Companion section — that is the norm."""
    assert deck_service.effective_companion(None, "1 Sol Ring\n") is None
    assert deck_service.effective_companion(None, None) is None


def test_companion_is_a_mutable_field() -> None:
    """PATCH reaches it via the same whitelist that drives the UPDATE."""
    assert "companion" in deck_service.MUTABLE_FIELDS


# ---- D19.34: set_head (promote) ----


@pytest.mark.asyncio
async def test_set_head_promotes_and_pins_then_rebuilds_deck_cards() -> None:
    version_row = {"version": 2, "branch": "deckbox", "decklist": "1 Sol Ring\n"}
    conn = _conn(_result([version_row]))
    row = await deck_service.set_head(conn, _DECK_ID, 2)
    assert row is not None
    assert row["version"] == 2
    assert "UPDATE decks SET head_version = :version, head_pinned = true" in _sql(conn, 1)
    assert "DELETE FROM deck_cards" in _sql(conn, 2)


@pytest.mark.asyncio
async def test_set_head_returns_none_for_an_unknown_version() -> None:
    conn = _conn(_result([]))
    assert await deck_service.set_head(conn, _DECK_ID, 99) is None
    # No UPDATE, no rebuild — nothing to promote to.
    assert conn.execute.await_count == 1


# ---- D19.37: set_head(version=None) un-pins ----


@pytest.mark.asyncio
async def test_set_head_none_clears_the_pin_and_resumes_auto_follow() -> None:
    version_row = _version_row(version=3, decklist="1 Sol Ring\n", branch="deckbox")
    conn = _conn(_result([version_row]))
    row = await deck_service.set_head(conn, _DECK_ID, None)
    assert row is not None
    assert row["version"] == 3
    assert row["branch"] == "deckbox"
    assert row["head_pinned"] is False
    update_sql = _sql(conn, 1)
    assert "head_version = NULL" in update_sql
    assert "head_pinned = false" in update_sql
    assert "DELETE FROM deck_cards" in _sql(conn, 2)


@pytest.mark.asyncio
async def test_set_head_none_returns_none_for_a_deck_with_no_versions() -> None:
    conn = _conn(_result([]))
    assert await deck_service.set_head(conn, _DECK_ID, None) is None
    # No UPDATE, no rebuild — nothing to resolve "current content" to.
    assert conn.execute.await_count == 1


def test_set_head_promote_returns_head_pinned_true() -> None:
    """The promote branch's return also carries `head_pinned`, matching the
    un-pin branch's shape — one contract for both, not two."""
    import inspect

    assert '"head_pinned": True' in inspect.getsource(deck_service.set_head)


# ---- D19.37: valid version sources ----


def test_valid_sources_mirrors_the_schema_check() -> None:
    assert deck_service.VALID_SOURCES == (
        "upload", "paste", "url", "edit", "restore", "fork",
    )


# ---- D19.34: no fourth "latest" definition ----


def test_head_aware_latest_definition_appears_exactly_three_times() -> None:
    """Pins the shape the D19.34 spec calls out by name: `_head_version_row`,
    `list_decks`'s LATERAL, and `rebuild_all_deck_cards`'s LATERAL are the
    ONLY three places allowed to define "current content" for a deck. Each
    carries the `# HEAD-AWARE "latest" definition` marker; a fourth
    ad-hoc "newest version" read anywhere else in this module would be the
    exact trap this deliverable exists to close, and would fail this test
    rather than fail silently in production."""
    import inspect

    source = inspect.getsource(deck_service)
    assert source.count('# HEAD-AWARE "latest" definition') == 3


@pytest.mark.asyncio
async def test_get_deck_reports_the_current_unresolved_count() -> None:
    """D19.47: the header must agree with the list of names beneath it.

    `deck_versions.unresolved_count` is a write-time snapshot that nothing
    recomputes, and `resolve_names` fuzzy-matches where the derived index
    does not — so after a parser fix the stored value goes stale and the two
    disagree. Live example: a deck whose index held 2 unresolved rows still
    stored 1, because the fuzzy fallback had rescued one of them.
    """
    version = {**_version_row(1), "unresolved_count": 1}
    conn = _conn(_result([DECK_ROW]), _result([version]), _result([{"n": 2}]))
    deck = await deck_service.get_deck(conn, _DECK_ID)
    assert deck is not None
    assert deck["unresolved_count"] == 2, "the stored 1 must not win over the index"
    sql = " ".join(str(c.args[0]) for c in conn.execute.call_args_list)
    assert "oracle_id IS NULL" in sql
    assert "::uuid" not in sql, "Bug.27: CAST, never ::"
