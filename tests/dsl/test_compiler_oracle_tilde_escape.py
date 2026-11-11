"""`o:~` substitution escapes ILIKE metachars in `name` (D10.3, §5.1).

`_compile_oracle` pastes `cards.name` into an ILIKE pattern via Postgres
`replace(value, '~', cards.name)`. Without escaping, a card name containing
`_`, `%`, or `\\` would be treated as ILIKE wildcards and match unrelated
rows. The fix wraps `cards.name` in three nested `replace(...)` calls
(backslash first, then `_`, then `%`) so the substituted text is matched
literally.

Scope is the `~`-substituted name slot only — the user-supplied `value` side
is NOT escaped here (pre-existing behavior, separate deliverable).
"""

from __future__ import annotations

from biblioplex.dsl.compiler import compile_ast
from biblioplex.dsl.parser import parse
from tests.dsl.test_compiler import cards_table, render


def test_o_tilde_escapes_name_metachars_in_rendered_sql() -> None:
    """The three nested `replace(...)` calls appear in compiled SQL."""
    cards = cards_table()
    sql = render(compile_ast(parse("o:~"), cards))
    # Backslash first: '\\' -> '\\\\'. SA 2.1 renders PG literals without
    # doubling backslashes, so these are the real SQL characters.
    assert "replace(cards.name, '\\', '\\\\')" in sql
    # Then `_` → `\_` (escape underscore single-char wildcard).
    assert "replace(replace(cards.name, '\\', '\\\\'), '_', '\\_')" in sql
    # Then `%` → `\%`. psycopg paramstyle doubles literal `%` in the
    # rendered SQL, so we look for `%%` on each side of the replace pair.
    assert (
        "replace(replace(replace(cards.name, '\\', '\\\\'), "
        "'_', '\\_'), '%%', '\\%%')"
    ) in sql


def test_o_tilde_escape_applied_even_for_plain_name() -> None:
    """Escape pipeline is unconditional — never elided based on the value.

    Regression guard: a future "optimize away the replaces when value has
    no metachars" change would silently re-introduce the bug for cards
    whose `name` (not the user's value) happens to contain `_`/`%`.
    """
    cards = cards_table()
    sql = render(compile_ast(parse('o:"draws a card named ~"'), cards))
    # Triple-replace wrappers must still be present.
    assert "replace(replace(replace(cards.name" in sql
    # And the user-side value is still passed through unmodified inside
    # the outer `replace(value, '~', escaped_name)`.
    assert "draws a card named ~" in sql


def test_o_without_tilde_branch_unchanged_by_escape_fix() -> None:
    """Non-`~` branch keeps the simple `oracle_search ILIKE '%value%'` shape.

    Regression guard for the `if "~" not in value` early return — the
    escape fix must not leak into the no-substitution path.
    """
    cards = cards_table()
    sql = render(compile_ast(parse("o:flying"), cards))
    assert "cards.name" not in sql
    assert "replace(" not in sql.lower()
    assert "oracle_search ILIKE" in sql
    # psycopg paramstyle renders literal `%` as `%%`.
    assert "'%%flying%%'" in sql
