"""Tests for the Search DSL tokenizer (D3.1, §5)."""

from __future__ import annotations

from hypothesis import assume, given
from hypothesis import strategies as st

from biblioplex.dsl.tokenizer import Token, TokenKind, tokenize


def kinds(toks: list[Token]) -> list[TokenKind]:
    return [t.kind for t in toks]


def values(toks: list[Token]) -> list[str]:
    return [t.value for t in toks]


def test_empty_query() -> None:
    assert tokenize("") == []
    assert tokenize("   \t  ") == []


def test_bare_word() -> None:
    toks = tokenize("lightning")
    assert kinds(toks) == [TokenKind.WORD]
    assert values(toks) == ["lightning"]


def test_multiple_bare_words() -> None:
    toks = tokenize("lightning bolt")
    assert kinds(toks) == [TokenKind.WORD, TokenKind.WORD]
    assert values(toks) == ["lightning", "bolt"]


def test_quoted_string() -> None:
    toks = tokenize('"lightning bolt"')
    assert len(toks) == 1
    assert toks[0].kind == TokenKind.QUOTED
    assert toks[0].value == "lightning bolt"


def test_quoted_with_escapes() -> None:
    toks = tokenize(r'"hello \"world\""')
    assert len(toks) == 1
    assert toks[0].kind == TokenKind.QUOTED
    assert toks[0].value == 'hello "world"'


def test_key_value_simple() -> None:
    toks = tokenize("t:creature")
    assert len(toks) == 1
    assert toks[0].kind == TokenKind.KEY_VALUE
    assert toks[0].key == "t"
    assert toks[0].value == "creature"


def test_key_value_quoted_value() -> None:
    toks = tokenize('o:"draw a card"')
    assert len(toks) == 1
    assert toks[0].kind == TokenKind.KEY_VALUE
    assert toks[0].key == "o"
    assert toks[0].value == "draw a card"
    assert toks[0].value_quoted is True


def test_key_value_comparator() -> None:
    toks = tokenize("cmc>=3")
    assert len(toks) == 1
    assert toks[0].kind == TokenKind.KEY_VALUE
    assert toks[0].key == "cmc"
    assert toks[0].op == ">="
    assert toks[0].value == "3"


def test_key_value_all_comparators() -> None:
    for op in [">=", "<=", "!=", "=", ">", "<", ":"]:
        toks = tokenize(f"cmc{op}3")
        assert len(toks) == 1
        assert toks[0].op == op, f"failed for op {op}"
        assert toks[0].key == "cmc"
        assert toks[0].value == "3"


def test_parens() -> None:
    toks = tokenize("(t:elf OR t:goblin)")
    assert kinds(toks) == [
        TokenKind.LPAREN,
        TokenKind.KEY_VALUE,
        TokenKind.OR,
        TokenKind.KEY_VALUE,
        TokenKind.RPAREN,
    ]


def test_or_keyword_case_insensitive() -> None:
    # `OR` in any case is the logical-OR operator.
    for src in ("a OR b", "a or b", "a Or b", "a oR b"):
        toks = tokenize(src)
        assert kinds(toks) == [TokenKind.WORD, TokenKind.OR, TokenKind.WORD], src


def test_negation_leading_dash() -> None:
    toks = tokenize("-t:creature")
    assert kinds(toks) == [TokenKind.NOT, TokenKind.KEY_VALUE]


def test_negation_bare_word() -> None:
    toks = tokenize("-goblin")
    assert kinds(toks) == [TokenKind.NOT, TokenKind.WORD]
    assert toks[1].value == "goblin"


def test_negation_quoted() -> None:
    toks = tokenize('-"draw a card"')
    assert kinds(toks) == [TokenKind.NOT, TokenKind.QUOTED]
    assert toks[1].value == "draw a card"


def test_regex_value() -> None:
    toks = tokenize("o:/draw \\d/")
    assert len(toks) == 1
    assert toks[0].kind == TokenKind.KEY_VALUE
    assert toks[0].value == "draw \\d"
    assert toks[0].value_regex is True


def test_bare_regex() -> None:
    toks = tokenize("/foo/")
    assert len(toks) == 1
    assert toks[0].kind == TokenKind.REGEX
    assert toks[0].value == "foo"


def test_exact_match_bang() -> None:
    toks = tokenize("!Lightning")
    assert len(toks) == 1
    assert toks[0].kind == TokenKind.EXACT
    assert toks[0].value == "Lightning"


def test_exact_match_bang_quoted() -> None:
    toks = tokenize('!"Lightning Bolt"')
    assert len(toks) == 1
    assert toks[0].kind == TokenKind.EXACT
    assert toks[0].value == "Lightning Bolt"


def test_mixed_complex_query() -> None:
    q = 't:creature c:rg -is:legendary (cmc<=3 OR pow>=4) o:"draw a card"'
    toks = tokenize(q)
    assert kinds(toks) == [
        TokenKind.KEY_VALUE,  # t:creature
        TokenKind.KEY_VALUE,  # c:rg
        TokenKind.NOT,
        TokenKind.KEY_VALUE,  # is:legendary
        TokenKind.LPAREN,
        TokenKind.KEY_VALUE,  # cmc<=3
        TokenKind.OR,
        TokenKind.KEY_VALUE,  # pow>=4
        TokenKind.RPAREN,
        TokenKind.KEY_VALUE,  # o:"draw a card"
    ]


def test_parens_no_space() -> None:
    toks = tokenize("(a)(b)")
    assert kinds(toks) == [
        TokenKind.LPAREN,
        TokenKind.WORD,
        TokenKind.RPAREN,
        TokenKind.LPAREN,
        TokenKind.WORD,
        TokenKind.RPAREN,
    ]


def test_dash_inside_word_is_not_negation() -> None:
    # Negation `-` only applies at the start of a token (after whitespace/paren
    # / start). A dash embedded in a bare word stays part of the word.
    toks = tokenize("life-link")
    assert len(toks) == 1
    assert toks[0].kind == TokenKind.WORD
    assert toks[0].value == "life-link"


def test_quoted_value_with_negation_keyword() -> None:
    # `-t:"creature - human"`
    toks = tokenize('-t:"creature - human"')
    assert kinds(toks) == [TokenKind.NOT, TokenKind.KEY_VALUE]
    assert toks[1].value == "creature - human"


# ---------- Hypothesis property tests ----------


@st.composite
def safe_word(draw: st.DrawFn) -> str:
    alphabet = st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"),
        max_codepoint=0x7E,
    )
    word = draw(st.text(alphabet=alphabet, min_size=1, max_size=10))
    # `OR` (any case) is a reserved boolean keyword in the DSL, not a bare word.
    assume(word.upper() != "OR")
    return word


@given(safe_word())
def test_property_bare_word_round_trip(w: str) -> None:
    toks = tokenize(w)
    assert len(toks) == 1
    assert toks[0].kind == TokenKind.WORD
    assert toks[0].value == w


@given(safe_word())
def test_property_quoted_word_round_trip(w: str) -> None:
    toks = tokenize(f'"{w}"')
    assert len(toks) == 1
    assert toks[0].kind == TokenKind.QUOTED
    assert toks[0].value == w


@given(st.lists(safe_word(), min_size=1, max_size=6))
def test_property_balanced_parens(words: list[str]) -> None:
    inner = " ".join(words)
    toks = tokenize(f"({inner})")
    assert toks[0].kind == TokenKind.LPAREN
    assert toks[-1].kind == TokenKind.RPAREN
    word_toks = toks[1:-1]
    assert all(t.kind == TokenKind.WORD for t in word_toks)
    assert [t.value for t in word_toks] == words


@given(st.lists(safe_word(), min_size=1, max_size=5))
def test_property_quote_preserves_spaces(words: list[str]) -> None:
    joined = " ".join(words)
    toks = tokenize(f'"{joined}"')
    assert len(toks) == 1
    assert toks[0].kind == TokenKind.QUOTED
    assert toks[0].value == joined


@given(safe_word().filter(lambda w: w.upper() != "OR"))
def test_property_negated_word(w: str) -> None:
    toks = tokenize(f"-{w}")
    assert kinds(toks) == [TokenKind.NOT, TokenKind.WORD]
    assert toks[1].value == w
