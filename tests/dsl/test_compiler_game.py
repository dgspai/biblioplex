"""`game:` operator tests (D3.16, §5.1).

`game:X` → `games_eff @> ARRAY[X]::text[]` against the per-oracle union of
every printing's ``games`` array (built at collapse time via a CTE — see
``ingest/collapse.py``). X is the Scryfall `games` enum: `paper`, `mtgo`,
`arena`, `astral`, `sega`. Value is lowercased; unknown values compile
(silent no-match at runtime). Cross-printing union means a card whose
first printing was paper-only but later picked up Arena still matches
``game:arena``.
"""

from __future__ import annotations

from biblioplex.dsl.compiler import compile_ast
from biblioplex.dsl.parser import parse
from tests.dsl.test_compiler import cards_table, render


def test_game_paper_compiles_to_games_eff_containment() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("game:paper"), cards))
    assert "games_eff" in sql
    assert "@>" in sql
    assert "'paper'" in sql
    # Must not regress to the pre-§5.1 raw-jsonb lookup.
    assert "raw -> 'games'" not in sql


def test_game_mtgo() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("game:mtgo"), cards))
    assert "'mtgo'" in sql
    assert "games_eff" in sql


def test_game_arena() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("game:arena"), cards))
    assert "'arena'" in sql
    assert "games_eff" in sql


def test_game_value_lowercased() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("game:Paper"), cards))
    assert "'paper'" in sql
    assert "'Paper'" not in sql


def test_game_combines_with_format() -> None:
    cards = cards_table()
    sql = render(compile_ast(parse("game:paper f:commander"), cards))
    assert "'paper'" in sql
    assert "'commander'" in sql
    assert " AND " in sql
