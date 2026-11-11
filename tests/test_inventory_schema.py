"""D18.1 — schema-level declarations for inventory and inventory_sync_settings."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"


@pytest.fixture(scope="module")
def schema_text() -> str:
    return SCHEMA_PATH.read_text()


def test_inventory_table_declared(schema_text: str) -> None:
    """inventory table declared with oracle_id FK, quantity, and card_name."""
    assert "CREATE TABLE IF NOT EXISTS inventory" in schema_text
    assert "oracle_id" in schema_text
    assert "quantity" in schema_text
    assert "card_name" in schema_text


def test_inventory_indexes_declared(schema_text: str) -> None:
    """inventory table indexes on oracle_id and lower(card_name)."""
    oracle_idx = re.compile(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+inventory_oracle_id_idx\s+ON\s+inventory",
        re.IGNORECASE,
    )
    card_name_idx = re.compile(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+inventory_card_name_lower_idx\s+ON\s+inventory",
        re.IGNORECASE,
    )
    assert oracle_idx.search(schema_text) is not None
    assert card_name_idx.search(schema_text) is not None


def test_inventory_sync_settings_table_declared(schema_text: str) -> None:
    """inventory_sync_settings table declared."""
    assert "CREATE TABLE IF NOT EXISTS inventory_sync_settings" in schema_text
    assert "deckbox_url" in schema_text


def test_wishlist_table_declared(schema_text: str) -> None:
    """D18.8 — wishlist is its own table, not a column on inventory."""
    assert "CREATE TABLE IF NOT EXISTS wishlist" in schema_text
    assert "REFERENCES cards(oracle_id) ON DELETE CASCADE" in schema_text
    # Wanted cards record "I want N", not a finish — no foil column.
    wishlist_ddl = schema_text.split("CREATE TABLE IF NOT EXISTS wishlist", 1)[1]
    wishlist_ddl = wishlist_ddl.split(");", 1)[0]
    assert "foil_quantity" not in wishlist_ddl
    assert "quantity" in wishlist_ddl


def test_wishlist_indexes_declared(schema_text: str) -> None:
    oracle_idx = re.compile(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+wishlist_oracle_id_idx\s+ON\s+wishlist",
        re.IGNORECASE,
    )
    name_idx = re.compile(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+wishlist_card_name_lower_idx\s+ON\s+wishlist",
        re.IGNORECASE,
    )
    assert oracle_idx.search(schema_text) is not None
    assert name_idx.search(schema_text) is not None


def test_uploads_table_declared(schema_text: str) -> None:
    """D18.17 — import history keeps each CSV verbatim for re-download."""
    assert "CREATE TABLE IF NOT EXISTS uploads" in schema_text
    uploads_ddl = schema_text.split("CREATE TABLE IF NOT EXISTS uploads", 1)[1]
    uploads_ddl = uploads_ddl.split(");", 1)[0]
    assert "content" in uploads_ddl and "bytea" in uploads_ddl
    assert "kind" in uploads_ddl
    # The CHECK is what keeps `kind` a two-valued enum without a lookup table.
    assert "IN ('inventory', 'wishlist')" in uploads_ddl
