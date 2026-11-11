"""Recursive-descent parser for the Biblioplex search DSL (tech_spec.md §5.3).

Grammar (precedence climbing, OR low, AND higher, NOT highest unary):

    expr      := or_expr
    or_expr   := and_expr ('OR' and_expr)*
    and_expr  := unary (unary)*           # implicit AND between adjacent
    unary     := '-' unary | primary
    primary   := '(' expr ')' | predicate
    predicate := KEY_VALUE | WORD | QUOTED | REGEX | EXACT

Single entrypoint: `parse(q: str) -> Expr`.

AST nodes are plain `@dataclass(frozen=True)` types — no pydantic, no
external deps. Compiler in `compiler.py` walks them.
"""

from __future__ import annotations

from dataclasses import dataclass

from biblioplex.dsl.tokenizer import Token, TokenKind, tokenize

__all__ = [
    "And",
    "BareWord",
    "Exact",
    "Expr",
    "KeyValue",
    "Not",
    "Or",
    "ParseError",
    "Regex",
    "parse",
]


# ----------------------------------------------------------------------------
# AST node types
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class BareWord:
    """A name-substring search (bare word or quoted phrase)."""

    value: str
    quoted: bool = False


@dataclass(frozen=True)
class Exact:
    """`!Word` / `!"Phrase"` — exact name match."""

    value: str


@dataclass(frozen=True)
class Regex:
    """Bare `/pattern/` regex against the name."""

    value: str


@dataclass(frozen=True)
class KeyValue:
    """A `key<op>value` predicate (e.g. `t:creature`, `cmc>=3`)."""

    key: str
    op: str
    value: str
    value_quoted: bool = False
    value_regex: bool = False


@dataclass(frozen=True)
class Not:
    """Logical negation (`-X`)."""

    child: Expr


@dataclass(frozen=True)
class And:
    """Implicit conjunction of two or more children."""

    children: tuple[Expr, ...]


@dataclass(frozen=True)
class Or:
    """Explicit disjunction (`OR`) of two or more children."""

    children: tuple[Expr, ...]


Expr = BareWord | Exact | Regex | KeyValue | Not | And | Or


# ----------------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------------


class ParseError(ValueError):
    """Raised when the token stream doesn't conform to the grammar."""


# ----------------------------------------------------------------------------
# Parser
# ----------------------------------------------------------------------------


class _Parser:
    """Mutable cursor over the token list. One instance per `parse` call."""

    def __init__(self, tokens: list[Token]) -> None:
        self._tokens = tokens
        self._i = 0

    # ---- cursor helpers ----

    def _peek(self) -> Token | None:
        return self._tokens[self._i] if self._i < len(self._tokens) else None

    def _eat(self) -> Token:
        t = self._tokens[self._i]
        self._i += 1
        return t

    def _eof(self) -> bool:
        return self._i >= len(self._tokens)

    # ---- grammar productions ----

    def parse(self) -> Expr:
        if self._eof():
            raise ParseError("empty query")
        ast = self._or()
        if not self._eof():
            stray = self._peek()
            assert stray is not None  # noqa: S101
            raise ParseError(f"unexpected token: {stray.kind.name}")
        return ast

    def _or(self) -> Expr:
        first = self._and()
        operands: list[Expr] = [first]
        while (tok := self._peek()) is not None and tok.kind is TokenKind.OR:
            self._eat()
            if self._eof() or self._peek_is_rparen():
                raise ParseError("dangling OR")
            operands.append(self._and())
        if len(operands) == 1:
            return operands[0]
        return Or(tuple(operands))

    def _and(self) -> Expr:
        first = self._unary()
        operands: list[Expr] = [first]
        while not self._eof():
            tok = self._peek()
            assert tok is not None  # noqa: S101
            if tok.kind in (TokenKind.RPAREN, TokenKind.OR):
                break
            operands.append(self._unary())
        if len(operands) == 1:
            return operands[0]
        return And(tuple(operands))

    def _unary(self) -> Expr:
        tok = self._peek()
        if tok is None:
            raise ParseError("expected predicate, got EOF")
        if tok.kind is TokenKind.NOT:
            self._eat()
            return Not(self._unary())
        return self._primary()

    def _primary(self) -> Expr:
        tok = self._peek()
        if tok is None:
            raise ParseError("expected predicate, got EOF")
        if tok.kind is TokenKind.LPAREN:
            self._eat()
            if self._peek_is_rparen():
                raise ParseError("empty parenthesised expression")
            inner = self._or()
            close = self._peek()
            if close is None or close.kind is not TokenKind.RPAREN:
                raise ParseError("missing closing paren")
            self._eat()
            return inner
        if tok.kind is TokenKind.RPAREN:
            raise ParseError("unexpected closing paren")
        if tok.kind is TokenKind.OR:
            raise ParseError("unexpected OR")
        # Terminal predicate.
        self._eat()
        if tok.kind is TokenKind.WORD:
            return BareWord(tok.value)
        if tok.kind is TokenKind.QUOTED:
            return BareWord(tok.value, quoted=True)
        if tok.kind is TokenKind.REGEX:
            return Regex(tok.value)
        if tok.kind is TokenKind.EXACT:
            return Exact(tok.value)
        if tok.kind is TokenKind.KEY_VALUE:
            return KeyValue(
                key=tok.key,
                op=tok.op,
                value=tok.value,
                value_quoted=tok.value_quoted,
                value_regex=tok.value_regex,
            )
        raise ParseError(f"unexpected token: {tok.kind.name}")

    def _peek_is_rparen(self) -> bool:
        tok = self._peek()
        return tok is not None and tok.kind is TokenKind.RPAREN


def parse(q: str) -> Expr:
    """Parse a search-DSL query string into an `Expr` AST.

    Raises:
        ParseError: on empty input, unbalanced parens, dangling operators.
    """
    tokens = tokenize(q)
    return _Parser(tokens).parse()
