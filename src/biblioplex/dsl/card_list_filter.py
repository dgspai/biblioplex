"""Filter for inventory and wishlist lists: search syntax or plain name text.

A query using at least one known search key ("t:dragon usd<2") runs through the
DSL. Anything else, or a DSL query that does not compile, stays a card-name
substring, so names like "Circle of Protection: Red" or "Fire // Ice" still match.
"""

from __future__ import annotations

from typing import Any, Protocol

import sqlalchemy as sa

from biblioplex.dsl.cards_table import CARDS
from biblioplex.dsl.compiler import ALL_KNOWN_KEYS, compile_ast
from biblioplex.dsl.parser import And, Expr, KeyValue, Not, Or, ParseError, parse


class _Connection(Protocol):
    async def execute(self, statement: Any, parameters: Any = None) -> Any: ...


def _uses_known_key(node: Expr) -> bool:
    if isinstance(node, KeyValue):
        return node.key.lower() in ALL_KNOWN_KEYS
    if isinstance(node, Not):
        return _uses_known_key(node.child)
    if isinstance(node, And | Or):
        return any(_uses_known_key(c) for c in node.children)
    return False


def dsl_select(q: str, table: str) -> sa.Select[Any] | None:
    """Oracle ids in `table` matching `q`, or None when `q` is name text."""
    try:
        ast = parse(q)
        if not _uses_known_key(ast):
            return None
        where = compile_ast(ast, CARDS)
    except (ParseError, ValueError):
        return None
    listed: sa.Select[Any] = sa.select(sa.column("oracle_id")).select_from(sa.table(table))
    return sa.select(CARDS.c.oracle_id).where(where, CARDS.c.oracle_id.in_(listed))


async def card_list_where(
    conn: _Connection, q: str | None, alias: str, table: str, params: dict[str, Any]
) -> str:
    """WHERE clause for `table AS alias`; adds its bind values to `params`."""
    q = (q or "").strip()
    if not q:
        return ""
    stmt = dsl_select(q, table)
    if stmt is None:
        params["q"] = f"%{q}%"
        return f"WHERE {alias}.card_name ILIKE :q "
    params["ids"] = list((await conn.execute(stmt)).scalars().all())
    return f"WHERE {alias}.oracle_id = ANY(CAST(:ids AS uuid[])) "
