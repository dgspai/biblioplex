"""D19.1 — schema-level declarations for decks and deck_versions."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"


@pytest.fixture(scope="module")
def schema_text() -> str:
    return SCHEMA_PATH.read_text()


def _ddl_of(schema_text: str, table: str) -> str:
    """Return the column block of one CREATE TABLE statement."""
    body = schema_text.split(f"CREATE TABLE IF NOT EXISTS {table}", 1)[1]
    return body.split(");", 1)[0]


def test_decks_table_declared(schema_text: str) -> None:
    """decks carries identity + user-owned metadata, no card content."""
    assert "CREATE TABLE IF NOT EXISTS decks" in schema_text
    ddl = _ddl_of(schema_text, "decks")
    for column in (
        "name", "format", "commander", "companion", "notes",
        "created_at", "updated_at",
    ):
        assert column in ddl, column


def test_decks_has_no_decklist_column(schema_text: str) -> None:
    """Current content IS the latest deck_versions row — never a second copy.

    A `decks.decklist` column would be a cache of `deck_versions.decklist`
    with no mechanism keeping the two honest, so an edit that updated one
    and not the other would silently detach a deck from its own history.
    """
    ddl = _ddl_of(schema_text, "decks")
    # Matches a COLUMN DECLARATION, not the bare word: comments inside the
    # DDL legitimately mention the decklist (D19.11 added one). A real
    # `decklist text NOT NULL,` column still trips this.
    assert re.search(r"(?mi)^\s*decklist\s+\w", ddl) is None


def test_decks_service_url_columns_declared(schema_text: str) -> None:
    """Three fixed services, three nullable columns — not a links table."""
    ddl = _ddl_of(schema_text, "decks")
    for column in ("deckbox_url", "moxfield_url", "archidekt_url"):
        assert column in ddl, column


def test_deck_versions_table_declared(schema_text: str) -> None:
    """deck_versions holds every state the decklist has been in."""
    assert "CREATE TABLE IF NOT EXISTS deck_versions" in schema_text
    ddl = _ddl_of(schema_text, "deck_versions")
    assert "REFERENCES decks(id) ON DELETE CASCADE" in ddl
    for column in (
        "version",
        "decklist",
        "filename",
        "source",
        "size_bytes",
        "total_cards",
        "distinct_cards",
        "unresolved_count",
        "created_at",
    ):
        assert column in ddl, column


def test_deck_versions_keeps_raw_upload_bytes(schema_text: str) -> None:
    """`raw_content` is nullable bytea — the original file, verbatim.

    Nullable because paste / edit / restore versions have no source file;
    bytea because a download must hand back the exact CSV or HTML that was
    imported, which the canonicalised text cannot reproduce.
    """
    ddl = _ddl_of(schema_text, "deck_versions")
    assert re.search(r"raw_content\s+bytea", ddl) is not None
    assert not re.search(r"raw_content\s+bytea\s+NOT NULL", ddl)


def test_deck_versions_source_is_check_constrained(schema_text: str) -> None:
    """`source` is a closed set via CHECK, same trick as `uploads.kind`."""
    ddl = _ddl_of(schema_text, "deck_versions")
    assert "IN ('upload', 'paste', 'url', 'edit', 'restore', 'fork')" in ddl


def test_deck_versions_number_is_unique_per_deck(schema_text: str) -> None:
    """(deck_id, version) is the natural key — no duplicate version numbers."""
    ddl = _ddl_of(schema_text, "deck_versions")
    assert re.search(r"UNIQUE\s*\(\s*deck_id\s*,\s*version\s*\)", ddl) is not None


def test_deck_versions_index_declared(schema_text: str) -> None:
    """Latest-version lookup and the history listing share one index."""
    idx = re.compile(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+deck_versions_deck_idx\s+"
        r"ON\s+deck_versions\s*\(\s*deck_id\s*,\s*version\s+DESC\s*\)",
        re.IGNORECASE,
    )
    assert idx.search(schema_text) is not None


def test_decks_companion_is_a_nullable_override(schema_text: str) -> None:
    """D19.11 — `companion` mirrors `commander`: optional, user-set override.

    Nullable because it is normally derived from the versioned decklist's
    `Companion` section; the column only exists for exports that carry no
    such section.
    """
    ddl = _ddl_of(schema_text, "decks")
    assert re.search(r"companion\s+text", ddl) is not None
    assert not re.search(r"companion\s+text\s+NOT NULL", ddl)


def test_decks_folder_column_declared(schema_text: str) -> None:
    """D19.16 — folders are a path string on the deck, not a folders table."""
    ddl = _ddl_of(schema_text, "decks")
    assert re.search(r"(?mi)^\s*folder\s+text", ddl) is not None


def test_decks_folder_index_uses_pattern_ops(schema_text: str) -> None:
    """`text_pattern_ops` is load-bearing, not decoration.

    Subtree listing runs `folder LIKE 'x/%'`. A default btree opclass does
    not serve a prefix LIKE, so the index would be built and never used —
    same reasoning as `cards_name_lower_pattern_idx`.
    """
    idx = re.compile(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+decks_folder_idx\s+"
        r"ON\s+decks\s*\(\s*folder\s+text_pattern_ops\s*\)",
        re.IGNORECASE,
    )
    assert idx.search(schema_text) is not None


def test_deck_cards_index_table_declared(schema_text: str) -> None:
    """D19.19 — derived index over the decklist text, not a second truth."""
    assert "CREATE TABLE IF NOT EXISTS deck_cards" in schema_text
    ddl = _ddl_of(schema_text, "deck_cards")
    assert "REFERENCES decks(id) ON DELETE CASCADE" in ddl
    assert "IN ('commander', 'main', 'sideboard')" in ddl
    # Nullable on purpose: an unresolved name must still count toward the
    # coverage denominator, or every percentage silently inflates.
    assert re.search(r"oracle_id\s+uuid\s*,", ddl) is not None
    assert re.search(r"oracle_id\s+uuid\s+NOT NULL", ddl) is None


def test_deck_versions_branch_and_message_declared(schema_text: str) -> None:
    """D19.33 — git-like branch label plus commit-style message per version."""
    ddl = _ddl_of(schema_text, "deck_versions")
    assert re.search(r"(?mi)^\s*branch\s+text\s+NOT NULL\s+DEFAULT\s+'main'", ddl) is not None
    assert re.search(r"(?mi)^\s*message\s+text\s*,", ddl) is not None
    assert not re.search(r"message\s+text\s+NOT NULL", ddl)


def test_deck_versions_branch_is_check_constrained(schema_text: str) -> None:
    """Closed set of four — a typo is a 422, not a phantom branch."""
    ddl = _ddl_of(schema_text, "deck_versions")
    assert "IN ('main', 'deckbox', 'moxfield', 'archidekt')" in ddl


def test_deck_versions_branch_index_declared(schema_text: str) -> None:
    """Per-branch latest-version lookup — D19.34's dedup and head auto-follow."""
    idx = re.compile(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+deck_versions_branch_idx\s+"
        r"ON\s+deck_versions\s*\(\s*deck_id\s*,\s*branch\s*,\s*version\s+DESC\s*\)",
        re.IGNORECASE,
    )
    assert idx.search(schema_text) is not None


def test_decks_head_columns_declared(schema_text: str) -> None:
    """D19.33 — `head_version` NULL means newest, so zero backfill on ship."""
    ddl = _ddl_of(schema_text, "decks")
    assert re.search(r"(?mi)^\s*head_version\s+integer\s*,", ddl) is not None
    assert not re.search(r"head_version\s+integer\s+NOT NULL", ddl)
    assert re.search(r"(?mi)^\s*head_pinned\s+boolean\s+NOT NULL\s+DEFAULT\s+false", ddl) is not None


def test_decks_trunk_branch_column_declared(schema_text: str) -> None:
    """D19.38 — nullable, `NULL` means "follow every import" (zero backfill)."""
    ddl = _ddl_of(schema_text, "decks")
    assert re.search(r"(?mi)^\s*trunk_branch\s+text\s*,", ddl) is not None
    assert not re.search(r"trunk_branch\s+text\s+NOT NULL", ddl)
