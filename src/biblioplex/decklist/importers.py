"""Coerce third-party deck exports into decklist text (D19.3).

Two of the nine reference exports in `data/example_deck_exports/` are not
decklists at all — Deckbox writes a deck-scoped CSV and a print-view HTML
page. Both are converted to the plaintext form here and then handed to
`parse_sections` (D19.2), so section handling has exactly one
implementation and adding a source never touches it.

Text that is already a decklist passes through BYTE-FOR-BYTE. Round-tripping
it through the parser and back would drop `//` grouping comments, which
people use to organise a list, and reorder nothing usefully in exchange.

Format is sniffed from content, never from a filename: uploads arrive with
whatever extension the browser guessed, and Deckbox's own export URL ends
in `?format=csv` regardless of what comes back (Bug.32).

ponytail: the HTML path is a tag-strip, not a parser. Replacing every tag
with a newline turns `<br>` separators AND the `<strong>Sideboard:</strong>`
divider into ordinary lines, and D19.2's colon-tolerant heading rule
already reads the latter — so this module needs no divider special-case
and no HTML dependency.
"""

from __future__ import annotations

import csv
import html
import io
import re

__all__ = ["to_decklist"]

_COUNT_KEYS = ("count", "quantity", "qty")
_NAME_KEYS = ("name", "card", "card name")
_SECTION_KEYS = ("section", "board")

# Deckbox writes `main` / `sideboard`. Anything unrecognised is mainboard —
# a row landing in the wrong section is recoverable, a dropped row is not.
_SECTION_VALUES = {
    "commander": "commander",
    "main": "main",
    "mainboard": "main",
    "deck": "main",
    "side": "sideboard",
    "sideboard": "sideboard",
}

# Whole elements whose TEXT is not deck content. `<title>` in particular
# sits before the card run and would otherwise parse as a card.
_NON_CONTENT_RE = re.compile(r"(?is)<(head|script|style)\b.*?</\1>")
_TAG_RE = re.compile(r"<[^<>]+>")
_HTML_HINTS = ("<br", "<html", "<!doctype html", "<body")


def _looks_like_html(raw: str) -> bool:
    head = raw[:4096].lower()
    return any(hint in head for hint in _HTML_HINTS)


def _header_of(raw: str) -> list[str] | None:
    """Return the first non-blank line's CSV fields, lowercased."""
    for line in raw.splitlines():
        if line.strip():
            fields = next(csv.reader([line]), [])
            return [f.strip().lower() for f in fields]
    return None


def _looks_like_csv(raw: str) -> bool:
    """A count-ish AND a name-ish column in the header, or it is not a CSV.

    Sniffing on a bare comma would misroute every decklist containing a
    legendary creature — `1 Radagast, the Brown` is a comma-bearing line
    that is emphatically not a CSV row.
    """
    header = _header_of(raw)
    if not header:
        return False
    return any(k in header for k in _COUNT_KEYS) and any(k in header for k in _NAME_KEYS)


def _pick(header: dict[str, str], keys: tuple[str, ...]) -> str | None:
    return next((header[k] for k in keys if k in header), None)


def _qty_of(raw_qty: str | None) -> int:
    """A bad cell costs one quantity, not the whole import."""
    try:
        return int((raw_qty or "").strip())
    except ValueError:
        return 1


def _from_csv(raw: str) -> str:
    reader = csv.DictReader(io.StringIO(raw))
    header = {f.strip().lower(): f for f in (reader.fieldnames or []) if f}
    count_key = _pick(header, _COUNT_KEYS)
    name_key = _pick(header, _NAME_KEYS)
    section_key = _pick(header, _SECTION_KEYS)
    if name_key is None:
        return ""

    buckets: dict[str, list[str]] = {"commander": [], "main": [], "sideboard": []}
    for row in reader:
        # No `//`-spacing fix here: the emitted lines go through
        # `parse_sections` -> `parse_decklist`, which normalises (D19.8).
        name = (row.get(name_key) or "").strip()
        if not name:
            continue
        qty = _qty_of(row.get(count_key) if count_key else None)
        if qty <= 0:
            continue
        section = (row.get(section_key) or "").strip().lower() if section_key else ""
        buckets[_SECTION_VALUES.get(section, "main")].append(f"{qty} {name}")

    return _assemble(buckets)


def _assemble(buckets: dict[str, list[str]]) -> str:
    """Serialise buckets back to decklist text with the headings D19.2 reads.

    The `Deck` heading is emitted whenever a commander block precedes the
    mainboard, and only then: without it `parse_sections` keeps the current
    bucket until the next heading, so the whole mainboard would be swallowed
    into `commander`.
    """
    out: list[str] = []
    if buckets["commander"]:
        out += ["Commander", *buckets["commander"], "", "Deck"]
    out += buckets["main"]
    if buckets["sideboard"]:
        out += ["", "Sideboard", *buckets["sideboard"]]
    return "\n".join(out) + "\n" if out else ""


def _from_html(raw: str) -> str:
    stripped = _TAG_RE.sub("\n", _NON_CONTENT_RE.sub("", raw))
    lines = (html.unescape(line).strip() for line in stripped.splitlines())
    kept = [line for line in lines if line]
    return "\n".join(kept) + "\n" if kept else ""


def to_decklist(raw: str) -> str:
    """Return `raw` as decklist text, converting CSV and HTML exports.

    Plaintext input is returned unchanged. Output is always safe to feed to
    `parse_sections`.
    """
    if not raw.strip():
        return ""
    if _looks_like_html(raw):
        return _from_html(raw)
    if _looks_like_csv(raw):
        return _from_csv(raw)
    return raw
