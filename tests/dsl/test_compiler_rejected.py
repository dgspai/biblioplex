"""Out-of-DSL operator rejection (D3.15, §5.2).

Reject every key from the §5.2 list with `UnsupportedOperatorError`. Endpoint
layer (M4) catches this and returns HTTP 422. The error message must read
`unsupported operator: <key>` verbatim.
"""

from __future__ import annotations

import pytest

from biblioplex.dsl.compiler import UnsupportedOperatorError, compile_ast
from biblioplex.dsl.parser import parse
from tests.dsl.test_compiler import cards_table

# D15.33 — `set`, `year`, `a`, `artist`, `flavor`, `frame` promoted out.
# D18.4 — `in` (in:inventory) promoted out.
_REJECTED_KEYS = [
    "s", "e", "cn", "b", "block", "g", "st", "new",
    "prints", "sets", "paperprints", "papersets", "cube",
    "date", "eur", "tix", "cheapest",
    "artists", "ft", "wm", "watermark",
    "border", "stamp", "art", "atag",
    "lang", "language",
]


@pytest.mark.parametrize("key", _REJECTED_KEYS)
def test_rejected_key(key: str) -> None:
    cards = cards_table()
    with pytest.raises(UnsupportedOperatorError) as exc:
        compile_ast(parse(f"{key}:foo"), cards)
    assert f"unsupported operator: {key}" == str(exc.value)


@pytest.mark.parametrize(
    "value",
    [
        "foil", "nonfoil", "etched", "glossy", "hires", "digital",
        "alchemy", "rebalanced", "promo", "spotlight", "booster",
    ],
)
def test_rejected_is_values(value: str) -> None:
    cards = cards_table()
    with pytest.raises(UnsupportedOperatorError) as exc:
        compile_ast(parse(f"is:{value}"), cards)
    assert str(exc.value) == f"unsupported operator: is:{value}"


def test_unknown_operator_still_raises() -> None:
    cards = cards_table()
    with pytest.raises(UnsupportedOperatorError):
        compile_ast(parse("xyznotanop:value"), cards)
