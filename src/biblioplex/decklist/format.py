"""D14.13 — decklist formatter symmetric with :func:`parse_decklist`.

Round-trips through the parser: for every ``entries`` we accept, the
output re-parses to the same list of :class:`DeckEntry`. Non-goal:
lossless casing / annotation preservation from the ORIGINAL raw input
— canonical `qty name` per line is enough for Arena / MTGO paste-back.

Styles (mirrors §6.14):

* ``arena`` — ``<qty> <name> (<set>) <collector>`` when a printing hint
  is present via optional ``set`` / ``collector_number`` keys on the
  entry dict; falls back to the mtgo shape when hints absent. This is
  the format Arena's "Import Deck" expects.
* ``mtgo`` — ``<qty> <name>``. The plain form; also accepted by every
  paper/webapp deck manager we've encountered.
* ``txt`` — ``mtgo`` semantics, plus a ``Sideboard:`` divider row if
  the caller passes ``sideboard`` entries.

Non-goal: image-driven proxy PDF (out of scope — every mtg-proxy site
solves this).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

__all__ = ["format_decklist", "SUPPORTED_STYLES"]


SUPPORTED_STYLES: frozenset[str] = frozenset({"arena", "mtgo", "txt"})


def _fmt_line_arena(entry: Any) -> str:
    """Arena format: `qty name (SET) collector` when hints present."""
    qty = int(_get(entry, "qty", 1) or 1)
    name = str(_get(entry, "name", ""))
    set_code = _get(entry, "set", None)
    collector = _get(entry, "collector_number", None)
    if set_code and collector:
        return f"{qty} {name} ({str(set_code).upper()}) {collector}"
    return f"{qty} {name}"


def _fmt_line_mtgo(entry: Any) -> str:
    qty = int(_get(entry, "qty", 1) or 1)
    name = str(_get(entry, "name", ""))
    return f"{qty} {name}"


def _get(entry: Any, key: str, default: Any) -> Any:
    """Attr-or-dict getter — accepts DeckEntry OR dict OR arbitrary obj."""
    if isinstance(entry, dict):
        return entry.get(key, default)
    return getattr(entry, key, default)


def format_decklist(
    entries: Sequence[Any],
    *,
    style: str = "mtgo",
    sideboard: Sequence[Any] | None = None,
) -> str:
    """Serialize ``entries`` (+ optional ``sideboard``) into a decklist string.

    Args:
        entries: iterable of ``DeckEntry`` OR dicts with ``qty`` / ``name``
            keys. Extra keys ``set`` / ``collector_number`` inform the
            Arena style; ignored elsewhere.
        style: ``arena`` | ``mtgo`` | ``txt``. Unknown styles raise
            ``ValueError`` — caller responsibility to validate.
        sideboard: only meaningful for ``txt`` (prepends a
            ``Sideboard:`` divider). Ignored for other styles — Arena
            has its own convention (blank-line separator) that we don't
            preserve, since the round-trip guarantee only covers the
            mainboard.

    Returns:
        A newline-joined decklist string. No trailing newline.
    """
    if style not in SUPPORTED_STYLES:
        raise ValueError(f"unsupported style: {style!r}")

    fmt = _fmt_line_arena if style == "arena" else _fmt_line_mtgo
    lines = [fmt(e) for e in entries]

    if style == "txt" and sideboard:
        lines.append("")
        lines.append("Sideboard:")
        lines.extend(fmt(e) for e in sideboard)

    return "\n".join(lines)
