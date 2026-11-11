"""Tokenizer for the Biblioplex search DSL (tech_spec.md §5).

The tokenizer scans a raw query string and emits a list of `Token` objects.
The grammar it serves is in `parser.py`. This module is intentionally a pure
lexer — it doesn't know about operator semantics.

Token kinds emitted:

- `WORD`     — bare unquoted word (treated as a name-substring search later).
- `QUOTED`   — `"..."` quoted string, escapes `\\` and `\"`.
- `KEY_VALUE`— `key<op>value` where op ∈ `:`, `=`, `!=`, `>`, `<`, `>=`, `<=`.
- `REGEX`    — `/.../` with no preceding key (matches `name_search` later).
- `EXACT`    — `!word` or `!"phrase"` for exact-name match.
- `NOT`      — leading `-` negation.
- `OR`       — literal `OR` keyword, case-insensitive (`OR`, `or`, `Or`).
- `LPAREN`   — `(`.
- `RPAREN`   — `)`.

`AND` is implicit (adjacent tokens). The lexer does not emit an AND token.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

__all__ = ["Token", "TokenKind", "tokenize", "TokenizeError"]


class TokenKind(Enum):
    WORD = auto()
    QUOTED = auto()
    KEY_VALUE = auto()
    REGEX = auto()
    EXACT = auto()
    NOT = auto()
    OR = auto()
    LPAREN = auto()
    RPAREN = auto()


@dataclass(frozen=True)
class Token:
    """A single lexical token.

    Attributes:
        kind: token classification.
        value: literal payload (for KEY_VALUE this is the value half).
        key: only set when `kind == KEY_VALUE`, the lhs of the operator.
        op: only set when `kind == KEY_VALUE`, one of `:`, `=`, `!=`, `>`,
            `<`, `>=`, `<=`.
        value_quoted: True when the value half came from a `"..."` literal.
        value_regex: True when the value half came from a `/.../` literal.
    """

    kind: TokenKind
    value: str = ""
    key: str = ""
    op: str = ""
    value_quoted: bool = False
    value_regex: bool = False


class TokenizeError(ValueError):
    """Raised when the input cannot be lexed (unterminated quote/regex)."""


# Order matters — longer prefixes first so `>=` beats `>`.
_COMPARATORS: tuple[str, ...] = (">=", "<=", "!=", ">", "<", "=", ":")

# Characters that terminate a bare word.
_WORD_TERMINATORS = set(" \t\n\r()")


def tokenize(source: str) -> list[Token]:
    """Lex `source` into a list of `Token`.

    Raises:
        TokenizeError: on unterminated string or regex literals.
    """
    tokens: list[Token] = []
    i = 0
    n = len(source)

    while i < n:
        ch = source[i]

        if ch.isspace():
            i += 1
            continue

        if ch == "(":
            tokens.append(Token(TokenKind.LPAREN))
            i += 1
            continue

        if ch == ")":
            tokens.append(Token(TokenKind.RPAREN))
            i += 1
            continue

        if ch == "-":
            tokens.append(Token(TokenKind.NOT))
            i += 1
            continue

        if ch == "!":
            # Exact match: !word or !"phrase".
            i += 1
            if i < n and source[i] == '"':
                value, i = _read_quoted(source, i)
                tokens.append(Token(TokenKind.EXACT, value=value, value_quoted=True))
            else:
                value, i = _read_bare_word(source, i)
                tokens.append(Token(TokenKind.EXACT, value=value))
            continue

        if ch == "/":
            # Bare regex.
            value, i = _read_regex(source, i)
            tokens.append(Token(TokenKind.REGEX, value=value, value_regex=True))
            continue

        if ch == '"':
            value, i = _read_quoted(source, i)
            tokens.append(Token(TokenKind.QUOTED, value=value, value_quoted=True))
            continue

        # Either a key-value pair, an OR keyword, or a bare word.
        key, op, key_end = _try_read_key_op(source, i)
        if key is not None and op is not None:
            i = key_end
            # Read the value half.
            if i < n and source[i] == '"':
                value, i = _read_quoted(source, i)
                tokens.append(
                    Token(
                        kind=TokenKind.KEY_VALUE,
                        key=key,
                        op=op,
                        value=value,
                        value_quoted=True,
                    )
                )
            elif i < n and source[i] == "/":
                value, i = _read_regex(source, i)
                tokens.append(
                    Token(
                        kind=TokenKind.KEY_VALUE,
                        key=key,
                        op=op,
                        value=value,
                        value_regex=True,
                    )
                )
            else:
                value, i = _read_bare_word(source, i)
                tokens.append(
                    Token(
                        kind=TokenKind.KEY_VALUE,
                        key=key,
                        op=op,
                        value=value,
                    )
                )
            continue

        # Bare word path.
        value, i = _read_bare_word(source, i)
        if value.upper() == "OR":
            tokens.append(Token(TokenKind.OR))
        else:
            tokens.append(Token(TokenKind.WORD, value=value))

    return tokens


def _read_quoted(source: str, i: int) -> tuple[str, int]:
    """Read a `"..."` string starting at `source[i] == '"'`.

    Supports `\\` and `\"` escapes. Returns (value, index_after_closing_quote).
    """
    assert source[i] == '"'  # noqa: S101
    i += 1
    chars: list[str] = []
    while i < len(source):
        ch = source[i]
        if ch == "\\" and i + 1 < len(source):
            nxt = source[i + 1]
            if nxt in ('"', "\\"):
                chars.append(nxt)
                i += 2
                continue
            chars.append(ch)
            i += 1
            continue
        if ch == '"':
            return "".join(chars), i + 1
        chars.append(ch)
        i += 1
    raise TokenizeError("unterminated quoted string")


def _read_regex(source: str, i: int) -> tuple[str, int]:
    """Read `/.../` regex starting at `source[i] == '/'`.

    Supports `\\/` escape. Returns (pattern, index_after_closing_slash).
    """
    assert source[i] == "/"  # noqa: S101
    i += 1
    chars: list[str] = []
    while i < len(source):
        ch = source[i]
        if ch == "\\" and i + 1 < len(source):
            nxt = source[i + 1]
            if nxt == "/":
                chars.append("/")
                i += 2
                continue
            chars.append(ch)
            chars.append(nxt)
            i += 2
            continue
        if ch == "/":
            return "".join(chars), i + 1
        chars.append(ch)
        i += 1
    raise TokenizeError("unterminated regex literal")


def _read_bare_word(source: str, i: int) -> tuple[str, int]:
    """Read a bare word until a terminator (whitespace, paren) or EOF."""
    start = i
    while i < len(source) and source[i] not in _WORD_TERMINATORS:
        i += 1
    return source[start:i], i


def _try_read_key_op(source: str, i: int) -> tuple[str | None, str | None, int]:
    """If `source[i:]` looks like `key<op>...`, return (key, op, index_after_op).

    Otherwise return (None, None, i). A key is any non-empty sequence of
    ASCII letters / digits / underscore. The op is the longest match from
    `_COMPARATORS`. Bare `OR` should not be classified as a key.
    """
    j = i
    while j < len(source) and (source[j].isalnum() or source[j] == "_"):
        j += 1
    if j == i:
        return None, None, i
    # Found a candidate key.
    for op in _COMPARATORS:
        if source.startswith(op, j):
            return source[i:j], op, j + len(op)
    return None, None, i
