"""D10.10 — schema-level index declarations for ``/cards/named``.

Two query paths in ``src/biblioplex/routers/cards.py`` lack supporting indexes
on ``cards``:

* exact mode runs ``LOWER(name) = LOWER(:name)`` and needs a functional
  index on ``lower(name)``;
* fuzzy mode runs ``similarity(name_sort_key, :name) >= 0.4`` and needs a
  trigram GIN on ``name_sort_key`` (``pg_trgm``'s ``similarity()`` cannot
  use a plain btree — the existing ``cards_name_sort_key_idx`` only backs
  equality + ``ORDER BY name_sort_key``).

These tests parse ``schema.sql`` as text — no live Postgres, no
``EXPLAIN``. The schema file is the single source of truth (no Alembic,
per the existing architecture), so a text-level assertion is enough to
pin the contract that the two indexes ship and have the right shape.
A live ``EXPLAIN``-backed check would require standing up Postgres in
the test runner, which is not worth the infra cost for what is purely a
schema-file declaration.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"


@pytest.fixture(scope="module")
def schema_text() -> str:
    return SCHEMA_PATH.read_text()


def test_cards_name_lower_idx_declared(schema_text: str) -> None:
    """Exact-mode lookup gets a functional btree on ``lower(name)``."""
    assert "cards_name_lower_idx" in schema_text


def test_cards_name_lower_idx_shape(schema_text: str) -> None:
    """The functional index must target ``cards (lower(name))``."""
    pattern = re.compile(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+cards_name_lower_idx\s+"
        r"ON\s+cards\s*\(\s*lower\s*\(\s*name\s*\)\s*\)\s*;",
        re.IGNORECASE,
    )
    assert pattern.search(schema_text) is not None, (
        "expected `CREATE INDEX IF NOT EXISTS cards_name_lower_idx "
        "ON cards (lower(name));` in schema.sql"
    )


def test_cards_name_sort_key_trgm_declared(schema_text: str) -> None:
    """Fuzzy-mode lookup gets a trgm GIN on ``name_sort_key``."""
    assert "cards_name_sort_key_trgm" in schema_text


def test_cards_name_sort_key_trgm_shape(schema_text: str) -> None:
    """The GIN index must target ``name_sort_key gin_trgm_ops``."""
    pattern = re.compile(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+cards_name_sort_key_trgm\s+"
        r"ON\s+cards\s+USING\s+gin\s*\(\s*name_sort_key\s+gin_trgm_ops\s*\)\s*;",
        re.IGNORECASE,
    )
    assert pattern.search(schema_text) is not None, (
        "expected `CREATE INDEX IF NOT EXISTS cards_name_sort_key_trgm "
        "ON cards USING gin (name_sort_key gin_trgm_ops);` in schema.sql"
    )


def test_existing_name_sort_key_btree_preserved(schema_text: str) -> None:
    """Regression guard: the new GIN must coexist with the btree.

    The btree backs equality and ``ORDER BY name_sort_key`` for
    ``order=name`` (§6.1, D4.2); the GIN backs ``pg_trgm`` similarity.
    Dropping the btree would silently regress sort performance.
    """
    assert "cards_name_sort_key_idx" in schema_text
    pattern = re.compile(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+cards_name_sort_key_idx\s+"
        r"ON\s+cards\s*\(\s*name_sort_key\s*\)\s*;",
        re.IGNORECASE,
    )
    assert pattern.search(schema_text) is not None


def test_cards_embedding_pending_partial_index_declared(schema_text: str) -> None:
    """D10.14: partial index on cards (id) WHERE embedding IS NULL.

    The embed loop selects `WHERE embedding IS NULL ORDER BY id LIMIT :b`
    once per batch. Without a partial index this seq-scans an increasingly
    large NOT-NULL prefix as pages fill in — O(N/batch × N) total work.
    The partial index is O(1) rows once the NULL set is drained; the
    planner picks it automatically. IF NOT EXISTS so operators can apply
    the fragment against an already-populated DB.
    """
    pattern = re.compile(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+cards_embedding_pending_idx\s+"
        r"ON\s+cards\s*\(\s*id\s*\)\s+WHERE\s+embedding\s+IS\s+NULL\s*;",
        re.IGNORECASE,
    )
    assert pattern.search(schema_text) is not None, (
        "expected `CREATE INDEX IF NOT EXISTS cards_embedding_pending_idx "
        "ON cards (id) WHERE embedding IS NULL;` in schema.sql"
    )


def test_bug21_cards_name_lower_pattern_idx_declared(schema_text: str) -> None:
    """Bug.21: text_pattern_ops btree backs autocomplete `LOWER(name) LIKE 'q%'`.

    The default `cards_name_lower_idx` uses the default operator class,
    which does NOT back LIKE prefix patterns — the planner would have to
    seq-scan on every autocomplete keystroke. A companion index with the
    `text_pattern_ops` opclass lets the planner turn a prefix LIKE into
    an index range scan.
    """
    pattern = re.compile(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+cards_name_lower_pattern_idx\s+"
        r"ON\s+cards\s*\(\s*lower\s*\(\s*name\s*\)\s+text_pattern_ops\s*\)\s*;",
        re.IGNORECASE,
    )
    assert pattern.search(schema_text) is not None, (
        "expected `CREATE INDEX IF NOT EXISTS cards_name_lower_pattern_idx "
        "ON cards (lower(name) text_pattern_ops);` in schema.sql"
    )


def test_combos_prereqs_trgm_declared(schema_text: str) -> None:
    """D16.7: pg_trgm GIN on combos.prereqs backs `/combos/search?q=`."""
    pattern = re.compile(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+combos_prereqs_trgm\s+"
        r"ON\s+combos\s+USING\s+gin\s*\(\s*prereqs\s+gin_trgm_ops\s*\)\s*;",
        re.IGNORECASE,
    )
    assert pattern.search(schema_text) is not None, (
        "expected combos_prereqs_trgm gin_trgm_ops declaration"
    )


def test_combos_steps_trgm_declared(schema_text: str) -> None:
    """D16.7: pg_trgm GIN on combos.steps backs `/combos/search?q=`."""
    pattern = re.compile(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+combos_steps_trgm\s+"
        r"ON\s+combos\s+USING\s+gin\s*\(\s*steps\s+gin_trgm_ops\s*\)\s*;",
        re.IGNORECASE,
    )
    assert pattern.search(schema_text) is not None, (
        "expected combos_steps_trgm gin_trgm_ops declaration"
    )


def test_d16_3_oracle_text_hash_column_declared(schema_text: str) -> None:
    """D16.3: delta-refresh column ``oracle_text_hash text NOT NULL DEFAULT ''``.

    Backs `embed/batch.py:SELECT_PENDING_SQL`'s new
    ``OR oracle_text_hash IS DISTINCT FROM md5(oracle_search)`` clause so
    rows whose oracle text is unchanged since last embed are skipped on
    subsequent refreshes. Default ``''`` guarantees the first refresh
    after an in-place ALTER re-embeds every row (empty hash != any md5).
    """
    pattern = re.compile(
        r"oracle_text_hash\s+text\s+NOT\s+NULL\s+DEFAULT\s+''",
        re.IGNORECASE,
    )
    assert pattern.search(schema_text) is not None, (
        "expected `oracle_text_hash text NOT NULL DEFAULT ''` on `cards`"
    )


def test_d15_24_has_unlimited_copies_column_declared(schema_text: str) -> None:
    """D15.24: data-driven "any number of copies" exemption.

    Generated boolean column matches oracle text patterns for cards
    whose own rules override the format's default copy cap:

    * "A deck can have any number of cards named X" (Rat Colony…)
    * "A deck can have up to <word> cards named X" (Seven Dwarves…)

    Kept STORED so the regex fires once at collapse rather than
    per-row on every legality request. Declared without IF NOT EXISTS
    because it lives inside the ``CREATE TABLE`` body (schema.sql is
    the single source of truth; existing deployments use the ALTER
    recipe from the D15.24 commit body).
    """
    column_pattern = re.compile(
        r"has_unlimited_copies\s+boolean\s+GENERATED\s+ALWAYS\s+AS\s*\(",
        re.IGNORECASE,
    )
    assert column_pattern.search(schema_text) is not None, (
        "expected `has_unlimited_copies boolean GENERATED ALWAYS AS (...)"
        " STORED` on the cards table"
    )
    regex_pattern = re.compile(
        r"a deck can have \(any number of\|up to \[a-z\]\+\) cards named"
    )
    assert regex_pattern.search(schema_text) is not None, (
        "expected the has_unlimited_copies generator to cover both the "
        "'any number of' and 'up to <word>' phrasings"
    )
    assert "STORED" in schema_text.upper(), (
        "generated column must be STORED so the regex fires at collapse "
        "time, not per legality request"
    )


def test_new_indexes_use_if_not_exists(schema_text: str) -> None:
    """Both new statements must be ``IF NOT EXISTS`` so an operator can
    re-apply the relevant fragment against an existing populated DB and
    pick up the indexes without a destructive ``compose down -v`` reset.
    """
    lower_pattern = re.compile(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+cards_name_lower_idx",
        re.IGNORECASE,
    )
    trgm_pattern = re.compile(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+cards_name_sort_key_trgm",
        re.IGNORECASE,
    )
    assert lower_pattern.search(schema_text) is not None
    assert trgm_pattern.search(schema_text) is not None
