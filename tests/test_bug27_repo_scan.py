"""D19.14 — no `:param::type` anywhere in the SQL we hand to `text()`.

Bug.27 is not a style rule. SQLAlchemy's `text()` bind regex refuses to
match a name followed by `:`, so `:deck_oids::uuid[]` registers a bind
called `deck_oid` (note the swallowed `s`), the real parameter is dropped
at compile time, and the literal `:deck_oids` reaches Postgres — which
answers `syntax error at or near ":"`.

The existing pin in test_decks_synergy.py only inspected SQL containing
`tag_pairs`, so four other sites went unguarded and `/decks/combos`,
`/decks/bracket` and `/decks/upgrade` all returned 500. This scan covers
the whole package instead of one query.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import text

SRC = Path(__file__).resolve().parent.parent / "src" / "biblioplex"

# `:name::type` — a bind immediately followed by a Postgres cast.
_DOUBLE_COLON_CAST = re.compile(r":(\w+)::")


def _python_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_no_bind_is_followed_by_a_double_colon_cast() -> None:
    offenders: list[str] = []
    for path in _python_files():
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            # Skip prose: comments and docstring lines that merely QUOTE the
            # bad form while explaining the rule.
            if stripped.startswith("#"):
                continue
            for match in _DOUBLE_COLON_CAST.finditer(line):
                # `:X::TYPE` is the generic placeholder the rule is STATED
                # with (see routers/catalog.py). Prose that teaches the rule
                # correctly must quote the wrong form to name it.
                if match.group(1) == "X":
                    continue
                offenders.append(
                    f"{path.relative_to(SRC)}:{lineno} :{match.group(1)}::"
                )
    assert not offenders, (
        "Bug.27: use CAST(:x AS type), never :x::type — text() drops the "
        "bind and Postgres gets a literal colon:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT 1 WHERE x <@ CAST(:deck_oids AS uuid[])", "deck_oids"),
        ("SELECT 1 WHERE c.id = ANY(CAST(:deck_ids AS uuid[]))", "deck_ids"),
    ],
)
def test_cast_form_keeps_the_bind(sql: str, expected: str) -> None:
    assert expected in text(sql)._bindparams


def test_double_colon_form_loses_the_bind() -> None:
    """The mechanism, pinned so nobody 'simplifies' the CAST back.

    Demonstrates the actual failure: the supplied parameter vanishes at
    compile time. How the bind is lost varies by release (<=2.0.51 mangles
    it to `deck_oid`; 2.0.57 creates no bind at all), so only the
    version-independent outcome is pinned.
    """
    from sqlalchemy.dialects import postgresql

    broken = text("SELECT 1 WHERE x <@ :deck_oids::uuid[]")
    assert "deck_oids" not in broken._bindparams

    compiled = broken.compile(dialect=postgresql.dialect(paramstyle="pyformat"))
    assert compiled.construct_params({"deck_oids": ["x"]}) == {}, (
        "the caller's parameter is silently dropped"
    )


def test_the_four_repaired_sql_constants_bind_correctly() -> None:
    """Every module-level SQL constant naming a deck array must bind it."""
    from biblioplex.routers import decks

    for name in ("_COMBOS_SUBSET_SQL", "_COMBOS_COUNT_SQL"):
        sql = getattr(decks, name)
        assert "deck_oids" in text(sql)._bindparams, name
