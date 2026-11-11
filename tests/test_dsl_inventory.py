"""Unit tests for DSL inventory search predicates (D18.4)."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from biblioplex.dsl.compiler import compile_ast
from biblioplex.dsl.parser import parse


@pytest.fixture
def cards_table() -> sa.Table:
    return sa.table(
        "cards",
        sa.column("oracle_id"),
        sa.column("name"),
    )


def test_compile_in_inventory(cards_table: sa.Table) -> None:
    ast = parse("in:inventory")
    expr = compile_ast(ast, cards_table)
    sql = str(expr.compile(compile_kwargs={"literal_binds": True}))
    assert "EXISTS" in sql.upper()
    assert "inventory" in sql.lower()


def test_compile_is_owned(cards_table: sa.Table) -> None:
    ast = parse("is:owned")
    expr = compile_ast(ast, cards_table)
    sql = str(expr.compile(compile_kwargs={"literal_binds": True}))
    assert "EXISTS" in sql.upper()
    assert "inventory" in sql.lower()


def test_compile_is_unowned(cards_table: sa.Table) -> None:
    ast = parse("is:unowned")
    expr = compile_ast(ast, cards_table)
    sql = str(expr.compile(compile_kwargs={"literal_binds": True}))
    assert "NOT EXISTS" in sql.upper() or "NOT (" in sql.upper()


def test_compile_owned_numeric(cards_table: sa.Table) -> None:
    ast = parse("owned:>=2")
    expr = compile_ast(ast, cards_table)
    sql = str(expr.compile(compile_kwargs={"literal_binds": True}))
    assert "inventory" in sql.lower()
    assert ">= 2" in sql


def test_compile_in_wishlist(cards_table: sa.Table) -> None:
    ast = parse("in:wishlist")
    sql = str(compile_ast(ast, cards_table).compile(compile_kwargs={"literal_binds": True}))
    assert "EXISTS" in sql.upper()
    assert "wishlist" in sql.lower()
    # Must NOT consult inventory — wanting is not owning.
    assert "inventory" not in sql.lower()


def test_compile_is_wanted(cards_table: sa.Table) -> None:
    ast = parse("is:wanted")
    sql = str(compile_ast(ast, cards_table).compile(compile_kwargs={"literal_binds": True}))
    assert "EXISTS" in sql.upper()
    assert "wishlist" in sql.lower()


def test_compile_is_unwanted(cards_table: sa.Table) -> None:
    ast = parse("is:unwanted")
    sql = str(compile_ast(ast, cards_table).compile(compile_kwargs={"literal_binds": True}))
    assert "NOT EXISTS" in sql.upper() or "NOT (" in sql.upper()
    assert "wishlist" in sql.lower()


def test_compile_wanted_numeric_sums(cards_table: sa.Table) -> None:
    ast = parse("wanted:>=2")
    sql = str(compile_ast(ast, cards_table).compile(compile_kwargs={"literal_binds": True}))
    assert "wishlist" in sql.lower()
    assert "sum" in sql.lower()
    assert ">= 2" in sql


def test_owned_and_wanted_target_different_tables(cards_table: sa.Table) -> None:
    """Regression guard for the rejected `list_type`-column design."""
    owned = str(compile_ast(parse("owned:1"), cards_table).compile(
        compile_kwargs={"literal_binds": True})).lower()
    wanted = str(compile_ast(parse("wanted:1"), cards_table).compile(
        compile_kwargs={"literal_binds": True})).lower()
    assert "inventory" in owned and "wishlist" not in owned
    assert "wishlist" in wanted and "inventory" not in wanted


def test_unowned_but_wanted_composes(cards_table: sa.Table) -> None:
    """`is:unowned is:wanted` — the shopping list."""
    ast = parse("is:unowned is:wanted")
    sql = str(compile_ast(ast, cards_table).compile(compile_kwargs={"literal_binds": True}))
    assert "inventory" in sql.lower()
    assert "wishlist" in sql.lower()
