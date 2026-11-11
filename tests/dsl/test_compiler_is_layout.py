"""`is:` layout & mana-shape compiler tests (D3.8, §5.1).

`is:dfc|mdfc|transform|split|flip|leveler|meld|tdfc` → `layout =` mapping
(alias `tdfc` → `transform`). `is:hybrid` / `is:phyrexian` use Postgres
regex on ``mana_cost_search``.
"""

from __future__ import annotations

import pytest

from biblioplex.dsl.compiler import compile_ast
from biblioplex.dsl.parser import parse
from tests.dsl.test_compiler import cards_table, render


@pytest.mark.parametrize(
    ("value", "expected_layout"),
    [
        ("dfc", "transform"),
        ("mdfc", "modal_dfc"),
        ("transform", "transform"),
        ("split", "split"),
        ("flip", "flip"),
        ("leveler", "leveler"),
        ("meld", "meld"),
        ("tdfc", "transform"),
    ],
)
def test_is_layout_aliases(value: str, expected_layout: str) -> None:
    cards = cards_table()
    sql = render(compile_ast(parse(f"is:{value}"), cards))
    assert "layout" in sql
    assert f"'{expected_layout}'" in sql


def test_is_hybrid_regex_on_mana_cost_search() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("is:hybrid"), cards))
    assert "mana_cost_search" in sql
    assert "~" in sql
    # Hybrid pattern: a pair of WUBRG letters separated by '/'.
    assert "[WUBRG]" in sql


def test_is_phyrexian_regex_on_mana_cost_search() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("is:phyrexian"), cards))
    assert "mana_cost_search" in sql
    assert "~" in sql
    assert "/P" in sql
