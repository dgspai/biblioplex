"""User-value ILIKE metachar escape across `o:` / `t:` / bare word (D13.1, §5.1).

Twin fix to D10.3: the name-side `~` substitution was escaped in
`_compile_oracle`, but the user-supplied value on the non-`~` branch,
on `t:`, and on the bare-word / `name:` predicate was still pasted
verbatim into an ILIKE pattern. A value containing `_` (any-single-
char), `%` (any-run), or `\\` (default escape) would silently be
treated as a wildcard.

The escape is Python-side because the value is a Python string at
compile time — no SQL replace() gymnastics needed. Order matters:
backslash first so we don't double-escape our own added escape char.
"""

from __future__ import annotations

from biblioplex.dsl.compiler import _escape_ilike_value, compile_ast
from biblioplex.dsl.parser import parse
from tests.dsl.test_compiler import cards_table, render

# ---- unit test for the helper ----

def test_escape_helper_leaves_plain_text_unchanged() -> None:
    assert _escape_ilike_value("flying") == "flying"


def test_escape_helper_escapes_underscore() -> None:
    assert _escape_ilike_value("foo_bar") == "foo\\_bar"


def test_escape_helper_escapes_percent() -> None:
    assert _escape_ilike_value("50%") == "50\\%"


def test_escape_helper_backslash_first() -> None:
    # `\_` (literal backslash + underscore) must escape to `\\\_`, NOT
    # `\\\\_` — order matters: backslash first, then `_` picks up its
    # own escape without double-hitting the backslash we just added.
    assert _escape_ilike_value("\\_") == "\\\\\\_"


# ---- rendered-SQL pins for each ILIKE call site ----

def test_o_value_percent_is_escaped_in_rendered_sql() -> None:
    """`o:50%` — `%` becomes `\\%` in the ILIKE pattern (non-tilde branch).

    SQLAlchemy 2.1 renders backslashes in PG literals as-is
    (standard_conforming_strings), and psycopg paramstyle doubles each literal `%` — so
    `\\%` in the escaped value shows up as `\\%%` in the rendered text.
    """
    cards = cards_table()
    sql = render(compile_ast(parse("o:50%"), cards))
    assert "oracle_search ILIKE" in sql
    assert "'%%50\\%%%%'" in sql


def test_o_value_underscore_is_escaped_in_rendered_sql() -> None:
    """`o:foo_bar` — `_` becomes `\\_` in the ILIKE pattern."""
    cards = cards_table()
    sql = render(compile_ast(parse("o:foo_bar"), cards))
    assert "oracle_search ILIKE" in sql
    # `\_` renders as-is (no backslash doubling on 2.1).
    assert "'%%foo\\_bar%%'" in sql


def test_t_value_percent_is_escaped_in_rendered_sql() -> None:
    """`t:50%` — twin fix on the `t:` compile path."""
    cards = cards_table()
    sql = render(compile_ast(parse("t:50%"), cards))
    assert "type_search ILIKE" in sql
    assert "'%%50\\%%%%'" in sql


def test_bare_word_value_percent_is_escaped_in_rendered_sql() -> None:
    """Bare-word / `name:` — twin fix on the name-substring predicate."""
    cards = cards_table()
    sql = render(compile_ast(parse("50%"), cards))
    # Bare word compiles to `(name_search ILIKE ...) OR (name_search %> ...)`.
    # The ILIKE branch takes the escape; the `%>` trigram branch does not
    # (pg_trgm does not treat `_`/`%` specially).
    assert "name_search ILIKE" in sql
    assert "'%%50\\%%%%'" in sql


def test_o_tilde_branch_value_is_also_escaped() -> None:
    """`o:` `~` branch — user metachars in the value are ALSO escaped.

    Before D13.1 the `~` branch only escaped the name-side substitution;
    a user query like `o:"draws ~%"` still let the trailing `%` wildcard
    through. Now the value gets Python-side escaped before it's fed to
    the SQL replace() that substitutes the escaped name expression.
    """
    cards = cards_table()
    sql = render(compile_ast(parse('o:"~%"'), cards))
    # Triple-replace of `cards.name` still present (the D10.3 name-side
    # escape must NOT regress).
    assert "replace(replace(replace(cards.name" in sql
    # The escaped value literal appears as `'~\\%'` (wire form of `~\%`).
    assert "'~\\%%'" in sql


def test_o_plain_value_still_uses_trgm_friendly_shape() -> None:
    """Regression: plain `o:flying` must still render the compact ILIKE."""
    cards = cards_table()
    sql = render(compile_ast(parse("o:flying"), cards))
    assert "oracle_search ILIKE" in sql
    assert "'%%flying%%'" in sql
    # No SQL-side replace() calls — escape is Python-side here.
    assert "replace(" not in sql.lower()
