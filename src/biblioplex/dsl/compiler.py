"""AST → SQLAlchemy WHERE compiler for the search DSL (tech_spec.md §5).

Single entrypoint: `compile_ast(ast, cards) -> ColumnElement[bool]`.

Subsequent D3.* commits flesh out each operator (color sets, mana costs,
numerics, `is:`/`has:`/`f:`/`r:`/etc.) — this initial pass covers only:

- Bare words and quoted phrases → `name_search ILIKE %val%`.
- `name:` / `o:` (`oracle:`) / `t:` (`type:`) → `*_search ILIKE %val%`.
- `!Exact` / `!"Exact"` → `lower(name) = lower(val)`.
- Boolean composition: AND / OR / NOT and the implicit-AND tree shape.

Anything not yet implemented raises `UnsupportedOperatorError` (introduced
properly in D3.15).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from biblioplex.classify.rules import LABELS as _PRED_LABELS
from biblioplex.dsl.colors import parse_color_letters
from biblioplex.dsl.mana import GENERIC, parse_cost_symbols
from biblioplex.dsl.parser import (
    And,
    BareWord,
    Exact,
    Expr,
    KeyValue,
    Not,
    Or,
    Regex,
)
from biblioplex.scryfall import catalogs as _scryfall_catalogs

__all__ = ["compile_ast", "compile_rank", "UnsupportedOperatorError"]


# D11.1 — trigram word-similarity threshold for the fuzzy OR-clause on
# name searches. Mirrors `FUZZY_SIMILARITY_THRESHOLD` in
# `routers/cards.py` (used by `/cards/named?fuzzy=`) so both surfaces
# share one knob. The Postgres GUC `pg_trgm.word_similarity_threshold`
# must be SET to the same value at connection-init time (db.py) so the
# `%>` operator agrees with the inline threshold — otherwise the GIN
# index won't be used for the OR-clause.
NAME_WORD_SIMILARITY_THRESHOLD = 0.4


class UnsupportedOperatorError(ValueError):
    """Raised when the DSL query uses a key explicitly out-of-scope per §5.2.

    Defined here in D3.3 so downstream operators can raise it; the rejection
    list itself is populated in D3.15. M4 (endpoint) maps this to HTTP 422.
    """


def _escape_ilike_value(value: str) -> str:
    """Escape ILIKE metachars in a user-supplied value (D13.1).

    ``_`` (any single char), ``%`` (any run of chars), and ``\\`` (the
    default escape char) become wildcards if pasted verbatim into an
    ILIKE pattern. `o:50%` / `o:foo_bar` would otherwise match unrelated
    rows. Order matters: backslash first so the escape we just added is
    not itself double-escaped. Trigram / GIN index still applies to the
    escaped literal — Postgres treats `\\_` as a literal underscore for
    both the LIKE machinery and pg_trgm.
    """
    return value.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%")


# Aliases per §5.1.
_NAME_KEYS = frozenset({"name"})
_ORACLE_KEYS = frozenset({"o", "oracle", "fo", "fulloracle"})
_TYPE_KEYS = frozenset({"t", "type"})
_COLOR_KEYS = frozenset({"c", "color"})
_IDENTITY_KEYS = frozenset({"id", "identity"})
_COMMANDER_KEYS = frozenset({"commander"})
_MANA_KEYS = frozenset({"m", "mana"})
_CMC_KEYS = frozenset({"cmc", "mv", "manavalue"})
# D15.22 — numeric filter on the per-oracle EDHrec rank column. The
# column already backs `order=edhrec`; predicate parity lets agents
# filter to popularity ranges (`edhrec<10000` = "top 10k played").
_EDHREC_KEYS = frozenset({"edhrec", "edhrecrank"})
# D15.23 — numeric filter on the cheapest-printing USD price. Same
# column powers `order=usd` + the `budget_usd` gate on /decks/upgrade
# and /decks/manabase; predicate parity was overdue.
_USD_KEYS = frozenset({"usd"})
_POW_KEYS = frozenset({"pow", "power"})
_TOU_KEYS = frozenset({"tou", "toughness"})
_PT_KEYS = frozenset({"pt", "powtou"})
_LOY_KEYS = frozenset({"loy", "loyalty"})
_IS_KEYS = frozenset({"is"})
_FORMAT_KEYS = frozenset({"f", "format"})
_BANNED_KEYS = frozenset({"banned"})
_RESTRICTED_KEYS = frozenset({"restricted"})
_RARITY_KEYS = frozenset({"r", "rarity"})
_KW_KEYS = frozenset({"kw", "keyword"})
_PRODUCES_KEYS = frozenset({"produces", "prod"})
_HAS_KEYS = frozenset({"has"})
_LAYOUT_KEYS = frozenset({"layout"})
_GAME_KEYS = frozenset({"game"})
# §5.1 oracle tags (Scryfall Tagger). `function:` is the long-form alias
# Scryfall publishes alongside `otag:`. `atag:` (illustration tags) is
# still rejected — we don't ingest illustration tags yet.
_OTAG_KEYS = frozenset({"otag", "function"})
# D14.2 — Commander Spellbook combos side table. `combo:has` matches any
# combo membership; `combo:<slug>` matches a specific combo id.
_COMBO_KEYS = frozenset({"combo"})
# Sentinel value for `combo:has` — a card in any combo. Case-insensitive
# so users don't get 0 hits from `combo:HAS` typo.
_COMBO_HAS_SENTINEL = "has"
# template-classifier-plan.md Step 5 — rules-layer predicted labels.
# Whitelisted against the LABELS tuple so a typo (`pred:rmap`) surfaces
# as a 422 instead of a silent empty result set.
_PRED_KEYS = frozenset({"pred"})

_HAS_RAW_FIELD: dict[str, str] = {
    "indicator": "color_indicator",
    "watermark": "watermark",
}

# Operators listed in §5.2 as explicitly out-of-DSL. Endpoint layer (M4)
# catches `UnsupportedOperatorError` and returns HTTP 422.
# D15.33 promoted `set` / `year` / `released` / `artist` / `flavor` /
# `frame` out of the rejected set; each now has an oracle-collapse
# caveat (matches the OLDEST printing's field only — reprints in later
# sets don't surface via these keys).
_REJECTED_KEYS: frozenset[str] = frozenset(
    {
        # printing-level identifiers still out-of-scope
        "s", "e", "cn", "b", "block", "g", "st", "new",
        "prints", "sets", "paperprints", "papersets", "cube",

        # time / market (usd promoted to §5.1 D15.23)
        "date", "eur", "tix", "cheapest",
        # credits / watermark (flavor promoted D15.33)
        "artists", "ft", "wm", "watermark",
        # physical / finish (frame promoted D15.33)
        "border", "stamp",
        # illustration tags (oracle tags = `otag`/`function` are wired).
        "art", "atag",
        # i18n
        "lang", "language",
    }
)

# D15.33 — Scryfall-parity printing-level keys promoted into the DSL.
# Every one binds against `raw->>'<field>'` extracted from the OLDEST
# printing per oracle_id (see §1.3 collapse note). Under-count is the
# expected failure mode for reprint queries — documented in tech_spec.
_SET_KEYS = frozenset({"set"})
_YEAR_KEYS = frozenset({"year"})
_RELEASED_KEYS = frozenset({"released"})
_ARTIST_KEYS = frozenset({"a", "artist"})
_FLAVOR_KEYS = frozenset({"flavor"})
_FRAME_KEYS = frozenset({"frame"})

# D18.4 — Inventory & owned predicates.
_INVENTORY_KEYS = frozenset({"in", "inventory"})
_OWNED_KEYS = frozenset({"owned"})

# D18.9 — wishlist predicates. `wishlist` is a separate table from
# `inventory`, so wanting a card never registers as owning it.
_WANTED_KEYS = frozenset({"wanted"})

# D17.6 — union of every prefix the compiler recognizes (supported + §5.2
# rejected). Consumed by `POST /cards/search/validate` for the
# `did_you_mean` typo-correction hint.
ALL_KNOWN_KEYS: frozenset[str] = (
    _INVENTORY_KEYS
    | _OWNED_KEYS
    | _WANTED_KEYS
    | _NAME_KEYS
    | _ORACLE_KEYS
    | _TYPE_KEYS
    | _COLOR_KEYS
    | _IDENTITY_KEYS
    | _COMMANDER_KEYS
    | _MANA_KEYS
    | _CMC_KEYS
    | _EDHREC_KEYS
    | _USD_KEYS
    | _POW_KEYS
    | _TOU_KEYS
    | _PT_KEYS
    | _LOY_KEYS
    | _IS_KEYS
    | _FORMAT_KEYS
    | _BANNED_KEYS
    | _RESTRICTED_KEYS
    | _RARITY_KEYS
    | _KW_KEYS
    | _PRODUCES_KEYS
    | _HAS_KEYS
    | _LAYOUT_KEYS
    | _GAME_KEYS
    | _OTAG_KEYS
    | _COMBO_KEYS
    | _PRED_KEYS
    | _SET_KEYS
    | _YEAR_KEYS
    | _RELEASED_KEYS
    | _ARTIST_KEYS
    | _FLAVOR_KEYS
    | _FRAME_KEYS
    | _REJECTED_KEYS
)

# `is:<value>` values listed in §5.2 as out-of-DSL (printing-finish category).
_REJECTED_IS_VALUES: frozenset[str] = frozenset(
    {
        "foil", "nonfoil", "etched", "glossy", "hires", "digital",
        "alchemy", "rebalanced", "promo", "spotlight", "booster",
    }
)

# Ordered rarity rank per §5.1.
_RARITY_RANK: dict[str, int] = {
    "common": 0,
    "uncommon": 1,
    "rare": 2,
    "special": 3,
    "mythic": 4,
    "bonus": 5,
}

# Top-level Magic permanent types (per §5.1 / Scryfall convention).
_PERMANENT_TYPES: tuple[str, ...] = (
    "artifact",
    "creature",
    "enchantment",
    "land",
    "planeswalker",
    "battle",
)

# `is:<value>` → `layout = X` mapping per §5.1 (alias `tdfc`→`transform`,
# `dfc`→`transform`, `mdfc`→`modal_dfc`).
_IS_LAYOUT_MAP: dict[str, str] = {
    "dfc": "transform",
    "tdfc": "transform",
    "transform": "transform",
    "mdfc": "modal_dfc",
    "split": "split",
    "flip": "flip",
    "leveler": "leveler",
    "meld": "meld",
}

# Aliases for the "many colors" / "no colors" sentinel values.
_MULTICOLOR_VALUES = frozenset({"m", "multi", "multicolor", "multicolored"})
_COLORLESS_VALUES = frozenset({"c", "colorless"})

# D15.32 — `is:<value>` → `raw->>'set_type' = X` map. Values are Scryfall
# `set_type` slugs. `is:commander` is NOT here — it stays column-backed
# via `c.is_commander` (see line ~626). `is:token` here matches token-only
# printings via set_type; `layout = 'token'` (a different concept) is
# unmapped by design.
_IS_SET_TYPE_MAP: dict[str, str] = {
    "funny": "funny",
    "masters": "masters",
    "token": "token",
}

# Canonical WUBRG sort key for color-set equality (§5.4). Mirrors the
# ingest-side sort in `ingest/flatten.py:_colors_eff` so Postgres array `=`
# (which is ORDERED) matches set-equally on both sides. Both ends must use
# the same key — if you change one, change the other.
_WUBRG_INDEX = {c: i for i, c in enumerate("WUBRG")}


def compile_ast(ast: Expr, cards: sa.Table) -> sa.ColumnElement[bool]:
    """Compile an AST node into a SQLAlchemy boolean column expression.

    Args:
        ast: parsed `Expr` tree from `parser.parse`.
        cards: the SQLAlchemy `Table` whose columns the predicates reference.

    Returns:
        A `ColumnElement[bool]` suitable for `select.where(...)`.
    """
    return _compile(ast, cards)


def _compile(node: Expr, cards: sa.Table) -> sa.ColumnElement[bool]:
    match node:
        case BareWord(value=v):
            return _name_substring(cards, v)
        case Exact(value=v):
            return sa.func.lower(cards.c.name) == sa.func.lower(v)
        case Regex(value=v):
            return cards.c.name_search.op("~*")(v)
        case KeyValue():
            return _compile_kv(node, cards)
        case Not(child=child):
            return sa.not_(_compile(child, cards))
        case And(children=children):
            return sa.and_(*(_compile(c, cards) for c in children))
        case Or(children=children):
            return sa.or_(*(_compile(c, cards) for c in children))
    raise UnsupportedOperatorError(f"unsupported AST node: {type(node).__name__}")


def _compile_kv(node: KeyValue, cards: sa.Table) -> sa.ColumnElement[bool]:
    key = node.key.lower()
    if key in _REJECTED_KEYS:
        raise UnsupportedOperatorError(f"unsupported operator: {key}")
    if key in _NAME_KEYS:
        if node.value_regex:
            return cards.c.name_search.op("~*")(node.value)
        return _name_substring(cards, node.value)
    if key in _ORACLE_KEYS:
        if node.value_regex:
            return cards.c.oracle_search.op("~*")(node.value)
        return _compile_oracle(node, cards)
    if key in _TYPE_KEYS:
        if node.value_regex:
            return cards.c.type_search.op("~*")(node.value)
        return cards.c.type_search.ilike(f"%{_escape_ilike_value(node.value)}%")
    if key in _COLOR_KEYS:
        return _compile_color(node, cards.c.colors_eff)
    if key in _IDENTITY_KEYS:
        return _compile_color(node, cards.c.color_identity)
    if key in _MANA_KEYS:
        return _compile_mana(node, cards)
    if key in _CMC_KEYS:
        return _compile_cmc(node, cards)
    if key in _EDHREC_KEYS:
        return _compile_edhrec(node, cards)
    if key in _USD_KEYS:
        return _compile_usd(node, cards)
    if key in _POW_KEYS:
        return _compile_text_numeric(node, cards.c.power)
    if key in _TOU_KEYS:
        return _compile_text_numeric(node, cards.c.toughness)
    if key in _LOY_KEYS:
        return _compile_text_numeric(node, cards.c.loyalty)
    if key in _PT_KEYS:
        return _compile_pt(node, cards)
    if key in _IS_KEYS:
        return _compile_is(node, cards)
    if key in _FORMAT_KEYS:
        return _compile_legality(cards, node.value, "legal")
    if key in _BANNED_KEYS:
        return _compile_legality(cards, node.value, "banned")
    if key in _RESTRICTED_KEYS:
        return _compile_legality(cards, node.value, "restricted")
    if key in _RARITY_KEYS:
        return _compile_rarity(node, cards)
    if key in _KW_KEYS:
        return _compile_keyword(node, cards)
    if key in _PRODUCES_KEYS:
        return _compile_color(node, cards.c.produced_mana)
    if key in _HAS_KEYS:
        return _compile_has(node, cards)
    if key in _LAYOUT_KEYS:
        return cards.c.layout == node.value.strip().lower()
    if key in _GAME_KEYS:
        return _compile_game(node, cards)
    if key in _OTAG_KEYS:
        return _compile_otag(node, cards)
    if key in _COMBO_KEYS:
        return _compile_combo(node, cards)
    if key in _PRED_KEYS:
        return _compile_pred(node, cards)
    if key in _COMMANDER_KEYS:
        if node.op != ":":
            raise UnsupportedOperatorError(f"unsupported operator: commander{node.op}")
        return _compile_color(replace(node, op="<="), cards.c.color_identity)
    # D15.33 — Scryfall-parity printing-level keys.
    if key in _SET_KEYS:
        return sa.func.lower(cards.c.raw.op("->>")("set")) == node.value.strip().lower()
    if key in _YEAR_KEYS:
        return _compile_year(node, cards)
    if key in _RELEASED_KEYS:
        return _compile_released(node, cards)
    if key in _ARTIST_KEYS:
        return cards.c.raw.op("->>")("artist").ilike(
            f"%{_escape_ilike_value(node.value)}%"
        )
    if key in _FLAVOR_KEYS:
        return cards.c.raw.op("->>")("flavor_text").ilike(
            f"%{_escape_ilike_value(node.value)}%"
        )
    if key in _FRAME_KEYS:
        return cards.c.raw.op("->>")("frame") == node.value.strip().lower()
    if key in _INVENTORY_KEYS:
        value = node.value.strip().lower()
        if value in ("inventory", "collection"):
            return _compile_in_inventory(cards)
        if value in ("wishlist", "wants"):
            return _compile_in_wishlist(cards)
        raise UnsupportedOperatorError(f"unsupported in: value: {node.value}")
    if key in _OWNED_KEYS:
        return _compile_owned(node, cards)
    if key in _WANTED_KEYS:
        return _compile_wanted(node, cards)
    raise UnsupportedOperatorError(f"unsupported operator: {node.key}")


def _compile_year(node: KeyValue, cards: sa.Table) -> sa.ColumnElement[bool]:
    """`year:` numeric comparator on `EXTRACT(year FROM released_at)`.

    Non-numeric RHS → sa.false() (mirrors `edhrec:` shape). Oracle
    collapse caveat: matches the OLDEST printing only.
    """
    try:
        rhs = _parse_number(node.value)
    except ValueError:
        return sa.false()
    year_expr = sa.func.extract(
        "year", sa.cast(cards.c.raw.op("->>")("released_at"), sa.Date)
    )
    return _numeric_cmp(year_expr, node.op, rhs)


def _compile_released(
    node: KeyValue, cards: sa.Table
) -> sa.ColumnElement[bool]:
    """`released:` full-date comparator, `YYYY-MM-DD` RHS.

    Non-parseable RHS → sa.false(). Oracle collapse caveat: OLDEST
    printing only.
    """
    value = node.value.strip()
    # Basic YYYY-MM-DD validation to keep bad input out of the CAST.
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        return sa.false()
    date_expr = sa.cast(cards.c.raw.op("->>")("released_at"), sa.Date)
    target = sa.cast(sa.literal(value), sa.Date)
    if node.op in (":", "="):
        return date_expr == target
    if node.op == "!=":
        return date_expr != target
    if node.op == ">":
        return date_expr > target
    if node.op == ">=":
        return date_expr >= target
    if node.op == "<":
        return date_expr < target
    if node.op == "<=":
        return date_expr <= target
    raise UnsupportedOperatorError(f"unsupported released: op: {node.op}")


def _compile_color(node: KeyValue, column: sa.ColumnElement[list[str]]) -> sa.ColumnElement[bool]:
    """Compile a `c:`/`id:` predicate against the given array column (§5.4)."""
    op = node.op
    value = node.value.lower().strip()

    # Numeric count: `c=2`, `c>=3`, etc.
    if value.isdigit():
        n = int(value)
        cnt = sa.func.cardinality(column)
        return _numeric_cmp(cnt, op, n)

    # Multicolor / colorless sentinels (only meaningful with `:` per §5.4 list).
    if value in _MULTICOLOR_VALUES:
        return sa.func.cardinality(column) >= 2
    if value in _COLORLESS_VALUES:
        # `c:c` → cardinality(colors_eff) = 0.
        return sa.func.cardinality(column) == 0

    # WUBRG sort matches the ingest-side `colors_eff` order so `c=COLORS`
    # (Postgres array `=`, which is ordered) works as set equality per §5.4.
    # Non-WUBRG letters (e.g. `C` mixed via `parse_color_letters("CW")`) sort
    # to the end deterministically; ingest never stores `C` in `colors_eff`
    # (colorless rows are `[]`) so such queries match nothing — same as today.
    letters = sorted(
        parse_color_letters(value),
        key=lambda c: _WUBRG_INDEX.get(c, len(_WUBRG_INDEX)),
    )
    target = sa.cast(postgresql.array(letters), postgresql.ARRAY(sa.Text))

    # `:` and `>=` → superset; `=` equal; `<=` subset; `>`/`<` strict variants.
    if op in (":", ">="):
        return column.op("@>")(target)
    if op == "=":
        return column == target
    if op == "<=":
        return column.op("<@")(target)
    if op == ">":
        return sa.and_(column.op("@>")(target), column != target)
    if op == "<":
        return sa.and_(column.op("<@")(target), column != target)
    if op == "!=":
        return column != target
    raise UnsupportedOperatorError(f"unsupported color comparator: {op}")


def _compile_cmc(node: KeyValue, cards: sa.Table) -> sa.ColumnElement[bool]:
    """`cmc:`/`mv:`/`manavalue:` numeric comparison + `even`/`odd` parity."""
    value = node.value.strip().lower()
    if value == "even":
        return cards.c.cmc % 2 == 0
    if value == "odd":
        return cards.c.cmc % 2 == 1
    return _numeric_cmp(cards.c.cmc, node.op, _parse_number(value))


def _compile_edhrec(node: KeyValue, cards: sa.Table) -> sa.ColumnElement[bool]:
    """`edhrec:` / `edhrecrank:` — numeric comparator on ``edhrec_rank``.

    Non-numeric RHS compiles to :func:`sa.false` (mirrors ``pow=X``);
    NULL-ranked rows never match a positive numeric comparison via
    Postgres three-valued logic. Backed by ``cards_edhrec_rank_idx``.
    """
    try:
        rhs = _parse_number(node.value)
    except ValueError:
        return sa.false()
    return _numeric_cmp(cards.c.edhrec_rank, node.op, rhs)


def _compile_usd(node: KeyValue, cards: sa.Table) -> sa.ColumnElement[bool]:
    """`usd:` — numeric comparator on ``price_usd`` (cheapest printing).

    Column semantic: MIN over all printings of the same oracle_id (see
    D1.9 collapse). Non-numeric RHS compiles to :func:`sa.false`;
    NULL-priced rows never match positive comparisons. Backed by
    ``cards_price_usd_idx``.
    """
    try:
        rhs = _parse_number(node.value)
    except ValueError:
        return sa.false()
    return _numeric_cmp(cards.c.price_usd, node.op, rhs)


def text_to_numeric(col: sa.ColumnElement[str]) -> sa.ColumnElement[Any]:
    """Cast a text power/toughness/loyalty column to numeric; NULL on non-numeric.

    D15.9: factored from three duplicated call sites (``_compile_text_numeric``,
    ``_compile_pt`` ×2, and ``routers.cards._text_numeric_cast``). Non-numeric
    Magic values (``*``, ``X``, ``*+1``, ``1+*``) must compare as NULL so they
    never match a numeric predicate; the regex guard is ``^-?\\d+(\\.\\d+)?$``.
    """
    return sa.case(
        (col.op("~")(r"^-?\d+(\.\d+)?$"), sa.cast(col, sa.Numeric)),
        else_=sa.null(),
    )


def _compile_text_numeric(
    node: KeyValue, column: sa.ColumnElement[str]
) -> sa.ColumnElement[bool]:
    """Numeric comparator against a text column (power/toughness/loyalty).

    Non-numeric Magic values (``*``, ``X``, ``*+1``, ``1+*``) must compare
    as NULL — never match. A non-numeric query value (e.g. ``pow=X``) is
    rejected by returning a constant-false predicate so the whole search
    still compiles cleanly.
    """
    try:
        rhs = _parse_number(node.value)
    except ValueError:
        return sa.false()
    return _numeric_cmp(text_to_numeric(column), node.op, rhs)


def _compile_pt(node: KeyValue, cards: sa.Table) -> sa.ColumnElement[bool]:
    """`pt:` / `powtou:` numeric comparator against ``power + toughness``."""
    try:
        rhs = _parse_number(node.value)
    except ValueError:
        return sa.false()
    return _numeric_cmp(
        text_to_numeric(cards.c.power) + text_to_numeric(cards.c.toughness),
        node.op,
        rhs,
    )


def _compile_oracle(node: KeyValue, cards: sa.Table) -> sa.ColumnElement[bool]:
    """`o:` / `oracle:` ILIKE on `oracle_search`, with `~`→name substitution.

    Per §5.1, a literal `~` in the query value stands for the card's top-level
    `name`. Since the substitution is row-dependent, we run it in SQL via
    Postgres `replace`. Without `~`, we keep the simple ILIKE for clarity
    and to preserve trgm-index friendliness.

    On the `~` branch we also escape the three ILIKE metacharacters in
    `cards.name` before pasting it into the pattern — backslash (ILIKE's
    default escape) first, then `_` (any single char) and `%` (any run of
    chars). Without this, names containing those literals would be treated
    as wildcards and match unrelated rows (D10.3).

    The user-supplied value is also escaped for ILIKE metachars (D13.1)
    on both branches — `~` stays a literal since it's not an ILIKE
    metachar and the sub-`replace` below only rewrites the `~` slot.
    """
    value = node.value
    if "~" not in value:
        return cards.c.oracle_search.ilike(f"%{_escape_ilike_value(value)}%")
    # Escape ILIKE metachars in the row's `name` before substitution.
    # Order matters: backslash first, then `_`/`%`.
    escaped_name = sa.func.replace(
        sa.func.replace(
            sa.func.replace(cards.c.name, sa.literal("\\"), sa.literal("\\\\")),
            sa.literal("_"),
            sa.literal("\\_"),
        ),
        sa.literal("%"),
        sa.literal("\\%"),
    )
    # Escape the value's non-`~` metachars Python-side, then per-row
    # replace `~` with the SQL-escaped column expression.
    escaped_value = _escape_ilike_value(value)
    pattern = sa.literal("%") + sa.func.replace(
        sa.literal(escaped_value), "~", escaped_name
    ) + sa.literal("%")
    return cards.c.oracle_search.ilike(pattern)


def _compile_keyword(node: KeyValue, cards: sa.Table) -> sa.ColumnElement[bool]:
    """`kw:` / `keyword:` — case-insensitive `keywords @> ARRAY[X]`.

    Postgres array containment is case-sensitive. To stay case-insensitive
    without losing the GIN index, normalise BOTH sides to lower-case via a
    `lower(unnest(...))` rewrite: build a subquery on `unnest(keywords)`
    and assert `EXISTS (... WHERE lower(kw) = lower(:value))`.

    D15.30 — comma-separated multi-value form (`keyword:flying,trample`)
    compiles to AND across per-value EXISTS predicates (mirrors `otag:`
    compound behavior). Empty tokens (double-comma, trailing comma) are
    dropped silently.
    """
    raw = node.value.strip().lower()
    values = [v.strip() for v in raw.split(",") if v.strip()]
    if not values:
        return sa.false()

    def _kw_exists(val: str) -> sa.ColumnElement[bool]:
        kw = sa.func.unnest(cards.c.keywords).alias("kw")
        kw_col = sa.column("kw", sa.Text)
        return sa.exists(
            sa.select(sa.literal(1))
            .select_from(kw)
            .where(sa.func.lower(kw_col) == val)
        )

    if len(values) == 1:
        return _kw_exists(values[0])
    return sa.and_(*(_kw_exists(v) for v in values))


def _compile_has(node: KeyValue, cards: sa.Table) -> sa.ColumnElement[bool]:
    """`has:indicator` / `has:watermark` / `has:combo` — presence checks.

    D15.35: `has:combo` renders EXISTS over the `combos` side table on
    `cards.oracle_id = ANY(combos.oracle_ids)` — same GIN-backed shape
    as the D14.2 `combo:has` sentinel; `has:` is where agents look for
    membership predicates (Scryfall parity with `has:foil`).
    """
    value = node.value.strip().lower()
    if value == "combo":
        combos = sa.table("combos", sa.column("oracle_ids"))
        return sa.exists(
            sa.select(sa.literal(1))
            .select_from(combos)
            .where(cards.c.oracle_id == sa.func.any(combos.c.oracle_ids))
        )
    field = _HAS_RAW_FIELD.get(value)
    if field is None:
        raise UnsupportedOperatorError(f"unsupported has: value: {node.value}")
    return cards.c.raw.op("->>")(field).is_not(None)


def _compile_rarity(node: KeyValue, cards: sa.Table) -> sa.ColumnElement[bool]:
    """`r:` / `rarity:` ordered comparator per §5.1.

    Rank: common(0) < uncommon(1) < rare(2) < special(3) < mythic(4) < bonus(5).
    Comparison via a CASE rank expression on `rarity`. Equality / `!=` short-
    circuit to direct string compare for clarity (and so the rank CASE only
    shows up on truly ordered comparators).
    """
    value = node.value.strip().lower()
    if value not in _RARITY_RANK:
        # Unknown rarity → constant-false (matches Scryfall's no-match behavior).
        return sa.false()
    op = node.op
    if op in (":", "="):
        return cards.c.rarity == value
    if op == "!=":
        return cards.c.rarity != value
    rank_expr = sa.case(_RARITY_RANK, value=cards.c.rarity)
    target = _RARITY_RANK[value]
    return _numeric_cmp(rank_expr, op, target)


def _compile_legality(
    cards: sa.Table, format_name: str, status: str
) -> sa.ColumnElement[bool]:
    """`legalities->>format = status` — Scryfall stores lower-case format keys."""
    key = format_name.strip().lower()
    return cards.c.legalities.op("->>")(key) == status


def _compile_otag(node: KeyValue, cards: sa.Table) -> sa.ColumnElement[bool]:
    """`otag:X` / `function:X` — Scryfall oracle tag (Tagger).

    EXISTS against the ``oracle_tags`` side table on ``oracle_id`` /
    lowercased slug. Hierarchy is pre-flattened at ingest, so the lookup
    is a flat indexed equality — no recursive CTE per query.
    """
    value = node.value.strip().lower()
    ot = sa.table(
        "oracle_tags",
        sa.column("oracle_id"),
        sa.column("tag"),
    )
    return sa.exists(
        sa.select(sa.literal(1))
        .select_from(ot)
        .where(sa.and_(ot.c.oracle_id == cards.c.oracle_id, ot.c.tag == value))
    )


def _compile_combo(node: KeyValue, cards: sa.Table) -> sa.ColumnElement[bool]:
    """`combo:has` / `combo:<slug>` / `combo:"<card name>"` — Spellbook combo.

    EXISTS against the ``combos`` side table:

    * ``combo:has`` — card's ``oracle_id`` is a member of any combo's
      ``oracle_ids`` array. Sentinel value; case-insensitive so a
      shift-key slip doesn't produce a silent empty result. Only fires
      on BARE `has` (D15.34: quoted ``"has"`` escapes into card-name
      mode below).
    * ``combo:<slug>`` (bare RHS) — card's ``oracle_id`` is a member AND
      the combo's ``id`` equals the (lowercased) slug. D14.2 shape.
    * ``combo:"<card name>"`` (quoted RHS, D15.34) — card appears in a
      combo alongside the named card. The RHS resolves via an inline
      ``(SELECT oracle_id FROM cards WHERE LOWER(name) = LOWER(:x))``
      subquery; unresolved names fall through 3VL (NULL = ANY(array) →
      NULL → EXISTS false) with no compile-time DB call.

    The GIN index on ``combos.oracle_ids`` backs the ``= ANY(...)``
    lookup. Only the ``:`` operator is accepted; other comparators raise
    :class:`UnsupportedOperatorError` (422 at the router).
    """
    if node.op != ":":
        raise UnsupportedOperatorError(f"unsupported operator: combo{node.op}")
    value = node.value.strip().lower()
    combos = sa.table(
        "combos",
        sa.column("id"),
        sa.column("oracle_ids"),
    )
    membership = cards.c.oracle_id == sa.func.any(combos.c.oracle_ids)
    # D15.34: quoted RHS (even if literal "has") escapes into card-name
    # resolution, so `combo:"has"` searches for a card named "Has", not
    # the sentinel.
    if not node.value_quoted and value == _COMBO_HAS_SENTINEL:
        where_clause: sa.ColumnElement[bool] = membership
    elif node.value_quoted:
        # D15.34 — pair-membership: both cards appear in the same combo.
        # The subquery resolves the RHS card name to its oracle_id. If
        # no card matches, the subquery yields NULL and the outer
        # `= ANY(...)` returns NULL — EXISTS then evaluates false.
        other_oracle_id = (
            sa.select(cards.c.oracle_id)
            .where(sa.func.lower(cards.c.name) == sa.func.lower(value))
            .scalar_subquery()
        )
        other_membership = other_oracle_id == sa.func.any(combos.c.oracle_ids)
        where_clause = sa.and_(membership, other_membership)
    else:
        where_clause = sa.and_(combos.c.id == value, membership)
    return sa.exists(
        sa.select(sa.literal(1)).select_from(combos).where(where_clause)
    )


def _compile_pred(node: KeyValue, cards: sa.Table) -> sa.ColumnElement[bool]:
    """`pred:X` — rules-layer predicted label membership.

    Matches when ``X`` is one of the LABELS that scored >= 0.5 in
    ``classify_by_rules`` for this card (i.e. ``X`` is a member of
    ``cards.predicted_labels``). The GIN index on the column makes the
    ``@>`` lookup constant-time. Unknown labels surface as 422.
    """
    if node.op != ":":
        raise UnsupportedOperatorError(f"unsupported operator: pred{node.op}")
    value = node.value.strip().lower()
    if value not in _PRED_LABELS:
        raise UnsupportedOperatorError(f"unknown predicted label: {value}")
    target = sa.cast(postgresql.array([value]), postgresql.ARRAY(sa.Text))
    return cards.c.predicted_labels.op("@>")(target)


def _compile_game(node: KeyValue, cards: sa.Table) -> sa.ColumnElement[bool]:
    """`game:X` — Scryfall games-array membership unioned across printings.

    Reads ``cards.games_eff`` (§5.1): the per-oracle union of every
    printing's ``games`` array, built at collapse time from a CTE
    aggregate. So a card whose first printing was paper-only and later
    picked up Arena still matches ``game:arena``. Values: paper, mtgo,
    arena, astral, sega (lowercased; unknown values compile and silently
    miss). GIN index on ``games_eff`` backs the ``@>`` lookup.
    """
    value = node.value.strip().lower()
    target = sa.cast(postgresql.array([value]), postgresql.ARRAY(sa.Text))
    return cards.c.games_eff.op("@>")(target)


def _compile_is(node: KeyValue, cards: sa.Table) -> sa.ColumnElement[bool]:
    """Compile an `is:<value>` predicate (D3.7 + D3.8 cohorts).

    Per §5.1: the type-category checks (`permanent`, `spell`, `vanilla`,
    `frenchvanilla`) derive from TOP-LEVEL ``type_line`` / ``oracle_text``
    only — they do NOT face-flatten. This matches Scryfall's front-face-only
    semantics for type categories. (`t:` and `o:` keep face-aware behavior
    via the `_search` columns.)

    `is:<printing-finish>` values listed in §5.2 (`foil`, `nonfoil`, ...)
    raise `UnsupportedOperatorError` so the M4 endpoint can return 422.
    """
    value = node.value.strip().lower()
    if value in _REJECTED_IS_VALUES:
        raise UnsupportedOperatorError(f"unsupported operator: is:{value}")
    if value == "commander":
        return cards.c.is_commander.is_(sa.true())
    if value == "permanent":
        return sa.or_(*(_type_has(cards, t) for t in _PERMANENT_TYPES))
    if value == "spell":
        return sa.not_(_type_has(cards, "land"))
    if value == "vanilla":
        # Creature with NULL / empty oracle_text on the front face.
        return sa.and_(
            _type_has(cards, "creature"),
            sa.or_(cards.c.oracle_text.is_(None), cards.c.oracle_text == ""),
        )
    if value in _IS_LAYOUT_MAP:
        return cards.c.layout == _IS_LAYOUT_MAP[value]
    if value == "hybrid":
        # `{W/U}`-style: any pair of WUBRG letters separated by '/'.
        return cards.c.mana_cost_search.op("~")(r"\{[WUBRG]/[WUBRG]\}")
    if value == "phyrexian":
        # `{W/P}`-style: any WUBRGC letter followed by '/P'.
        return cards.c.mana_cost_search.op("~")(r"\{[WUBRGC]/P\}")
    if value == "frenchvanilla":
        return _compile_frenchvanilla(cards)
    if value == "reserved":
        # D15.20 — Reserved List membership. Oracle-level (RL is by
        # name, not printing), so our oldest-printing collapse preserves
        # it. `raw->>'reserved'` is Scryfall's boolean-cast-to-text
        # ('true' / 'false').
        return cards.c.raw.op("->>")("reserved") == "true"
    if value in ("banned", "restricted", "legal"):
        # D15.31 — legality-status predicates. `is:banned` etc. bind
        # against the ambient `commander` format (the highest-signal
        # default for the corpus's target audience). Explicit-format
        # form is the existing `banned:<fmt>` / `restricted:<fmt>` /
        # `f:<fmt>` keys.
        return _compile_legality(cards, "commander", value)
    if value in _IS_SET_TYPE_MAP:
        # D15.32 — set_type-based predicates. `is:funny` = Un-sets;
        # `is:masters` = reprint sets; `is:token` = token-only printings
        # (distinct from `layout = 'token'` which we don't map).
        return cards.c.raw.op("->>")("set_type") == _IS_SET_TYPE_MAP[value]
    if value in ("owned", "inventory"):
        return _compile_in_inventory(cards)
    if value in ("unowned", "not_owned"):
        return sa.not_(_compile_in_inventory(cards))
    if value in ("wanted", "wishlist"):
        return _compile_in_wishlist(cards)
    if value in ("unwanted", "not_wanted"):
        return sa.not_(_compile_in_wishlist(cards))
    raise UnsupportedOperatorError(f"unsupported is: value: {node.value}")


def _compile_in_inventory(cards: sa.Table) -> sa.ColumnElement[bool]:
    """`in:inventory` / `is:owned` — EXISTS in inventory with quantity > 0."""
    inv = sa.table("inventory", sa.column("oracle_id"), sa.column("quantity"))
    return sa.exists(
        sa.select(sa.literal(1))
        .select_from(inv)
        .where(sa.and_(inv.c.oracle_id == cards.c.oracle_id, inv.c.quantity > 0))
    )


def _compile_in_wishlist(cards: sa.Table) -> sa.ColumnElement[bool]:
    """`in:wishlist` / `is:wanted` — EXISTS in wishlist with quantity > 0."""
    wl = sa.table("wishlist", sa.column("oracle_id"), sa.column("quantity"))
    return sa.exists(
        sa.select(sa.literal(1))
        .select_from(wl)
        .where(sa.and_(wl.c.oracle_id == cards.c.oracle_id, wl.c.quantity > 0))
    )


def _compile_quantity(
    node: KeyValue,
    cards: sa.Table,
    table: str,
    exists: Callable[[sa.Table], sa.ColumnElement[bool]],
    truthy: tuple[str, ...],
    falsy: tuple[str, ...],
) -> sa.ColumnElement[bool]:
    """Shared `owned:` / `wanted:` compiler.

    Boolean RHS short-circuits to the EXISTS shape; anything else becomes a
    ``COALESCE(SUM(quantity), 0)`` comparison. SUM, not a row read — both
    tables hold one row per printing, so a playset split across two editions
    must still satisfy ``>=4``.
    """
    val = node.value.strip().lower()
    op = node.op

    # Tolerate both `owned>=2` and `owned:>=2`, like the rest of §5.1.
    if op in (":", "="):
        for prefix in (">=", "<=", "!=", ">", "<"):
            if val.startswith(prefix):
                op = prefix
                val = val[len(prefix) :].strip()
                break

    if op in (":", "="):
        if val in truthy:
            return exists(cards)
        if val in falsy:
            return sa.not_(exists(cards))

    try:
        rhs = _parse_number(val)
    except ValueError:
        # A typo narrows the result set instead of 422-ing a long query.
        return sa.false()

    tbl = sa.table(table, sa.column("oracle_id"), sa.column("quantity"))
    sum_expr = sa.func.coalesce(
        sa.select(sa.func.sum(tbl.c.quantity))
        .where(tbl.c.oracle_id == cards.c.oracle_id)
        .scalar_subquery(),
        0,
    )
    return _numeric_cmp(sum_expr, op, rhs)


def _compile_wanted(node: KeyValue, cards: sa.Table) -> sa.ColumnElement[bool]:
    """`wanted:N` / `wanted:true` / `wanted:false` — wishlist quantity filter."""
    return _compile_quantity(
        node,
        cards,
        table="wishlist",
        exists=_compile_in_wishlist,
        truthy=("true", "yes", "wanted", "1"),
        falsy=("false", "no", "unwanted", "0"),
    )


def _compile_owned(node: KeyValue, cards: sa.Table) -> sa.ColumnElement[bool]:
    """`owned:N` / `owned:true` / `owned:false` — quantity filter against inventory."""
    return _compile_quantity(
        node,
        cards,
        table="inventory",
        exists=_compile_in_inventory,
        truthy=("true", "yes", "owned", "1"),
        falsy=("false", "no", "unowned", "0"),
    )


def _type_has(cards: sa.Table, token: str) -> sa.ColumnElement[bool]:
    """Top-level ``type_line ILIKE %token%`` — case-insensitive substring."""
    return cards.c.type_line.ilike(f"%{token}%")


def _compile_frenchvanilla(cards: sa.Table) -> sa.ColumnElement[bool]:
    """`is:frenchvanilla` — creature whose oracle text is keyword-only.

    Preferred path: the Scryfall keyword catalogs (`keyword-abilities`,
    `keyword-actions`, `ability-words`) are snapshotted into
    :mod:`biblioplex.scryfall.catalogs` at app startup. When present, we
    strip parenthesised reminder text from ``oracle_text`` and check
    that every remaining token belongs to the catalog union — that's
    how "Flying, vigilance, lifelink" passes while "Whenever this
    creature attacks, …" does not.

    Fallback (catalog fetch failed at boot, or tests bypassed the
    lifespan): the legacy heuristic — no ``.``/``:``/``,`` in the
    oracle text — which is conservative but correct on vanilla +
    single-keyword cards.
    """
    pattern = _scryfall_catalogs.get_frenchvanilla_regex()
    if pattern:
        stripped = sa.func.regexp_replace(
            sa.func.coalesce(cards.c.oracle_text, sa.literal("")),
            sa.literal(r"\([^)]*\)"),
            sa.literal(""),
            sa.literal("g"),
        )
        return sa.and_(
            _type_has(cards, "creature"),
            sa.or_(
                cards.c.oracle_text.is_(None),
                cards.c.oracle_text == "",
                stripped.op("~*")(pattern),
            ),
        )
    return sa.and_(
        _type_has(cards, "creature"),
        sa.or_(
            cards.c.oracle_text.is_(None),
            cards.c.oracle_text == "",
            sa.not_(cards.c.oracle_text.op("~")(r"[.:,]")),
        ),
    )


def _parse_number(value: str) -> int | float:
    """Parse a DSL numeric literal into int or float; ValueError on garbage."""
    s = value.strip()
    if not s:
        raise ValueError("empty numeric value")
    # Try int first, then float.
    try:
        return int(s)
    except ValueError:
        return float(s)


def _compile_mana(node: KeyValue, cards: sa.Table) -> sa.ColumnElement[bool]:
    """Compile an `m:`/`m=`/`m>` predicate per §5.5.

    Semantics: tokenize the query value into a symbol multiset (generic
    numerals expanded to ``GENERIC`` pips). Then for the card, split
    ``mana_cost_search`` on whitespace into per-face cost strings and assert
    that AT LEAST ONE non-empty face's multiset matches:

    * ``:`` and ``>=`` — face is a multiset SUPERSET of the query.
    * ``=`` — face equals the query exactly.
    * ``>`` — face is a STRICT superset (superset AND face larger than query).

    Empty face costs (MDFC backs, transform backs at top level) are skipped.
    """
    op = node.op
    query_ms = parse_cost_symbols(node.value)

    # Build per-face predicate: a SQLAlchemy expression referencing `face_cost`.
    face_cost: sa.ColumnElement[str] = sa.literal_column("face_cost", sa.Text)

    superset_conjuncts = _superset_conjuncts(face_cost, query_ms)

    if op in (":", ">="):
        face_pred = sa.and_(face_cost != "", *superset_conjuncts)
    elif op == "=":
        # Equality: superset AND total face symbol count equals total query.
        query_total = sum(query_ms.values())
        face_pred = sa.and_(
            face_cost != "",
            _face_symbol_count(face_cost) == query_total,
            *superset_conjuncts,
        )
    elif op == ">":
        # Strict superset: superset AND face larger than query.
        query_total = sum(query_ms.values())
        face_pred = sa.and_(
            face_cost != "",
            _face_symbol_count(face_cost) > query_total,
            *superset_conjuncts,
        )
    else:
        raise UnsupportedOperatorError(f"unsupported mana comparator: {op}")

    # EXISTS over per-face split of `mana_cost_search`.
    face_split = (
        sa.select(sa.literal(1))
        .select_from(
            sa.func.regexp_split_to_table(cards.c.mana_cost_search, r"\s+").alias(
                "face_cost"
            )
        )
        .where(face_pred)
    )
    return sa.exists(face_split)


def _superset_conjuncts(
    face_cost: sa.ColumnElement[str],
    query_ms: dict[str, int],
) -> list[sa.ColumnElement[bool]]:
    """Return per-symbol "face contains >= k of symbol" predicates.

    Generic pips are counted by summing the numeric values of all ``{N}``
    symbols in the face cost. Specific symbols (W, U/B, W/P, etc.) are
    counted by literal substring occurrences of ``{SYM}``.
    """
    out: list[sa.ColumnElement[bool]] = []
    for symbol, required in query_ms.items():
        if required <= 0:
            continue
        if symbol == GENERIC:
            out.append(_face_generic_count(face_cost) >= required)
        else:
            out.append(_face_symbol_occurrences(face_cost, symbol) >= required)
    return out


def _face_symbol_occurrences(
    face_cost: sa.ColumnElement[str], symbol: str
) -> sa.ColumnElement[int]:
    """Count occurrences of literal ``{symbol}`` substring in the face cost."""
    needle = f"{{{symbol}}}"
    needle_len = len(needle)
    # (length(face) - length(replace(face, needle, ''))) / len(needle)
    diff = sa.func.length(face_cost) - sa.func.length(
        sa.func.replace(face_cost, needle, "")
    )
    return sa.cast(diff / needle_len, sa.Integer)


def _face_generic_count(face_cost: sa.ColumnElement[str]) -> sa.ColumnElement[int]:
    """Sum the integer values of all ``{N}`` numeric pips in the face cost.

    Uses Postgres ``regexp_matches(..., 'g')`` in a lateral set-returning
    position, casts each captured group to int, and SUMs them. Returns 0
    when there are no numeric pips (COALESCE wraps the SUM).
    """
    # regexp_matches in a FROM clause returns one row per match; the captured
    # group is the first array element.
    rm = sa.func.regexp_matches(face_cost, r"\{(\d+)\}", "g").table_valued(
        sa.column("captures", postgresql.ARRAY(sa.Text))
    )
    captures = rm.c.captures
    n = sa.cast(captures[1], sa.Integer)
    return sa.func.coalesce(
        sa.select(sa.func.sum(n)).select_from(rm).scalar_subquery(),
        sa.literal(0),
    )


def _face_symbol_count(face_cost: sa.ColumnElement[str]) -> sa.ColumnElement[int]:
    """Total expanded-multiset symbol count for a face cost.

    Generic ``{N}`` pips count as N; every other ``{...}`` symbol counts as 1.
    """
    # Total braced symbols = count of {...} groups.
    total_symbols = sa.func.coalesce(
        sa.func.array_length(sa.func.regexp_split_to_array(face_cost, r"\{[^}]+\}"), 1)
        - 1,
        sa.literal(0),
    )
    # Numeric pips contribute (N - 1) extra each (we already counted them as 1).
    numeric_total = _face_generic_count(face_cost)
    numeric_symbol_count = sa.func.coalesce(
        sa.func.array_length(sa.func.regexp_split_to_array(face_cost, r"\{\d+\}"), 1)
        - 1,
        sa.literal(0),
    )
    return total_symbols + numeric_total - numeric_symbol_count


def _numeric_cmp(
    lhs: sa.ColumnElement[int] | sa.ColumnElement[float],
    op: str,
    rhs: int | float,
) -> sa.ColumnElement[bool]:
    if op in (":", "="):
        return lhs == rhs
    if op == "!=":
        return lhs != rhs
    if op == ">":
        return lhs > rhs
    if op == ">=":
        return lhs >= rhs
    if op == "<":
        return lhs < rhs
    if op == "<=":
        return lhs <= rhs
    raise UnsupportedOperatorError(f"unsupported numeric comparator: {op}")


def _name_substring(cards: sa.Table, value: str) -> sa.ColumnElement[bool]:
    """Name-substring predicate: literal ILIKE OR trigram word-similarity.

    The fuzzy OR-clause uses the `%>` operator so the
    `cards_name_trgm` GIN index (`gin_trgm_ops` on `name_search`)
    backs both branches. The threshold is the session-level
    `pg_trgm.word_similarity_threshold` GUC, which db.py sets to
    `NAME_WORD_SIMILARITY_THRESHOLD` at connection init.
    """
    return sa.or_(
        cards.c.name_search.ilike(f"%{_escape_ilike_value(value)}%"),
        cards.c.name_search.op("%>")(value),
    )


def compile_rank(
    ast: Expr, cards: sa.Table
) -> sa.ColumnElement[float] | None:
    """Return a relevance expression for `order=relevance`, or None.

    Walks the AST collecting every term that hits ``_name_substring``
    (bare words, unkeyed quoted phrases, and ``name:`` KVs without
    `value_regex`). When at least one such term exists, returns
    ``word_similarity(<joined-terms>, cards.name_search)``. Filter-only
    queries (``t:legendary``, ``c:r``, ...) return None so the router
    can fall back to a deterministic sort.

    The router uses the returned expression as `ORDER BY ... DESC`. The
    SQL function is used (rather than the indexed ``%>`` operator) because
    Postgres can ORDER BY ``word_similarity`` directly — the GIN index is
    used to PRE-FILTER via the ``%>`` clause inside ``_name_substring``,
    and the ORDER BY only ranks rows that already cleared the threshold.
    """
    terms = _extract_bare_terms(ast)
    if not terms:
        return None
    return sa.func.word_similarity(sa.literal(" ".join(terms)), cards.c.name_search)


def _extract_bare_terms(node: Expr) -> list[str]:
    """Collect bare-word / quoted-phrase / `name:` values, in source order.

    Negated terms (`-invason`) are skipped — a user excluding a name
    doesn't want results ranked by similarity to it.
    """
    match node:
        case BareWord(value=v):
            return [v]
        case KeyValue() if node.key.lower() in _NAME_KEYS and not node.value_regex:
            return [node.value]
        case Not():
            return []
        case And(children=children) | Or(children=children):
            out: list[str] = []
            for child in children:
                out.extend(_extract_bare_terms(child))
            return out
    return []
