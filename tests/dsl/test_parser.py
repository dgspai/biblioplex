"""Tests for the search DSL recursive-descent parser (D3.2, §5.3)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from biblioplex.dsl.parser import (
    And,
    BareWord,
    Exact,
    KeyValue,
    Not,
    Or,
    ParseError,
    Regex,
    parse,
)


def test_empty_query_is_error() -> None:
    with pytest.raises(ParseError):
        parse("")


def test_single_bare_word() -> None:
    ast = parse("lightning")
    assert ast == BareWord("lightning")


def test_implicit_and_two_words() -> None:
    ast = parse("lightning bolt")
    assert ast == And((BareWord("lightning"), BareWord("bolt")))


def test_implicit_and_three_predicates() -> None:
    ast = parse("a b c")
    assert ast == And((BareWord("a"), BareWord("b"), BareWord("c")))


def test_key_value_predicate() -> None:
    ast = parse("t:creature")
    assert ast == KeyValue(key="t", op=":", value="creature")


def test_explicit_or() -> None:
    ast = parse("a OR b")
    assert ast == Or((BareWord("a"), BareWord("b")))


def test_or_precedence_below_and() -> None:
    # `a b OR c d` → (a AND b) OR (c AND d).
    ast = parse("a b OR c d")
    assert ast == Or(
        (
            And((BareWord("a"), BareWord("b"))),
            And((BareWord("c"), BareWord("d"))),
        )
    )


def test_parens_override_precedence() -> None:
    ast = parse("a (b OR c)")
    assert ast == And(
        (
            BareWord("a"),
            Or((BareWord("b"), BareWord("c"))),
        )
    )


def test_negation_unary() -> None:
    ast = parse("-t:creature")
    assert ast == Not(KeyValue(key="t", op=":", value="creature"))


def test_negation_double() -> None:
    ast = parse("--x")
    assert ast == Not(Not(BareWord("x")))


def test_negation_inside_and() -> None:
    ast = parse("a -b")
    assert ast == And((BareWord("a"), Not(BareWord("b"))))


def test_or_with_negation() -> None:
    ast = parse("a OR -b")
    assert ast == Or((BareWord("a"), Not(BareWord("b"))))


def test_quoted_value() -> None:
    ast = parse('o:"draw a card"')
    assert ast == KeyValue(key="o", op=":", value="draw a card", value_quoted=True)


def test_bare_quoted_string() -> None:
    ast = parse('"lightning bolt"')
    assert ast == BareWord("lightning bolt", quoted=True)


def test_regex_predicate() -> None:
    ast = parse("o:/draw \\d/")
    assert ast == KeyValue(
        key="o", op=":", value="draw \\d", value_regex=True
    )


def test_bare_regex() -> None:
    ast = parse("/foo/")
    assert ast == Regex("foo")


def test_exact_match() -> None:
    ast = parse("!Lightning")
    assert ast == Exact("Lightning")


def test_unbalanced_paren_open() -> None:
    with pytest.raises(ParseError):
        parse("(a OR b")


def test_unbalanced_paren_close() -> None:
    with pytest.raises(ParseError):
        parse("a OR b)")


def test_dangling_or() -> None:
    with pytest.raises(ParseError):
        parse("a OR")


def test_dangling_not() -> None:
    with pytest.raises(ParseError):
        parse("-")


def test_empty_parens() -> None:
    with pytest.raises(ParseError):
        parse("()")


def test_numeric_kv() -> None:
    ast = parse("cmc>=3")
    assert ast == KeyValue(key="cmc", op=">=", value="3")


def test_complex_scryfall_query() -> None:
    ast = parse('t:creature c:rg -is:legendary (cmc<=3 OR pow>=4)')
    assert ast == And(
        (
            KeyValue(key="t", op=":", value="creature"),
            KeyValue(key="c", op=":", value="rg"),
            Not(KeyValue(key="is", op=":", value="legendary")),
            Or(
                (
                    KeyValue(key="cmc", op="<=", value="3"),
                    KeyValue(key="pow", op=">=", value="4"),
                )
            ),
        )
    )


# ---------- Hypothesis property tests ----------


@st.composite
def safe_word(draw: st.DrawFn) -> str:
    alphabet = st.characters(
        whitelist_categories=("Ll", "Lu"),
        max_codepoint=0x7E,
    )
    w = draw(st.text(alphabet=alphabet, min_size=1, max_size=8))
    # Avoid the reserved logical-OR token (any case).
    return w + "x" if w.upper() == "OR" else w


@given(st.lists(safe_word(), min_size=1, max_size=5))
def test_property_implicit_and_chain(words: list[str]) -> None:
    ast = parse(" ".join(words))
    if len(words) == 1:
        assert ast == BareWord(words[0])
    else:
        assert isinstance(ast, And)
        assert ast.children == tuple(BareWord(w) for w in words)


@given(st.lists(safe_word(), min_size=2, max_size=5))
def test_property_explicit_or_chain(words: list[str]) -> None:
    ast = parse(" OR ".join(words))
    assert isinstance(ast, Or)
    assert ast.children == tuple(BareWord(w) for w in words)


@given(st.lists(safe_word(), min_size=1, max_size=4))
def test_property_paren_idempotent(words: list[str]) -> None:
    """Wrapping an expression in parens doesn't change its semantics."""
    base = " ".join(words)
    bare = parse(base)
    wrapped = parse(f"({base})")
    assert bare == wrapped


@given(safe_word())
def test_property_double_negation(w: str) -> None:
    ast = parse(f"--{w}")
    assert ast == Not(Not(BareWord(w)))
