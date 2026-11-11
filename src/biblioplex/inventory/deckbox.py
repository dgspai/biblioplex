"""Deckbox collection fetcher and CSV parser per D18.2."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass

import httpx

from biblioplex.decklist.parser import normalize_card_name

# Prefix marking a warning that means "this file is not a usable Deckbox
# CSV at all", as opposed to a per-row nit. The router refuses to touch the
# database when it sees one — a `mode: replace` on an unparseable file would
# delete the inventory and report success.
_FATAL = "fatal: "


def is_fatal_warning(warning: str) -> bool:
    """True when a `parse_deckbox_csv` warning means the file was unusable."""
    return warning.startswith(_FATAL)


@dataclass
class ParsedDeckboxItem:
    """Single item parsed from a Deckbox CSV export."""

    card_name: str

    quantity: int
    """TOTAL copies owned, foils included."""

    foil_quantity: int = 0
    """How many of ``quantity`` are foil. Deckbox lists foil and non-foil as
    separate rows sharing a (Name, Edition); they fold into one item here, so
    ``foil_quantity <= quantity`` always."""

    set_name: str | None = None
    set_code: str | None = None

    price_usd: float | None = None
    """Deckbox's price for THIS printing, in USD. Preferred over
    `cards.price_usd`, which is the cheapest printing across the whole
    oracle (D14.11) and therefore under-values an actual collection."""


def extract_deckbox_export_url(url_or_id: str) -> str:
    """Extract canonical Deckbox CSV export URL from a set URL or bare set ID.

    Accepts:
      - "3293466"
      - "https://deckbox.org/sets/3293466"
      - "https://deckbox.org/sets/3293466/export?format=csv"
      - "https://deckbox.org/sets/3293466?format=csv"

    Returns:
      "https://deckbox.org/sets/<set_id>/export?format=csv"
    """
    cleaned = url_or_id.strip()
    match = re.search(r"(\d+)", cleaned)
    if not match:
        raise ValueError(f"Invalid Deckbox URL or Set ID: '{url_or_id}'")
    set_id = match.group(1)
    return f"https://deckbox.org/sets/{set_id}/export?format=csv"


class DeckboxFetchError(RuntimeError):
    """Deckbox answered, but not with a CSV export."""


def _assert_csv_body(resp: httpx.Response, export_url: str) -> str:
    """Reject a non-CSV body before it can reach the parser.

    Deckbox serves `/sets/<id>/export` to anonymous callers as a 200 HTML
    LOGIN PAGE, not a 401. Left unchecked that HTML parses to zero cards,
    and a `mode: replace` sync would then cheerfully delete the whole
    inventory and report success. Fail loudly instead.
    """
    body = resp.text
    ctype = resp.headers.get("content-type", "")
    looks_html = body.lstrip()[:200].lower().startswith(("<!doctype", "<html"))
    if "html" in ctype.lower() or looks_html:
        raise DeckboxFetchError(
            f"{export_url} returned HTML, not CSV — Deckbox requires a logged-in "
            "session to export a set. Export the CSV from the Deckbox UI and "
            "upload it (or paste it) instead."
        )
    return body


async def fetch_deckbox_csv(
    url_or_id: str,
    client: httpx.AsyncClient | None = None,
) -> tuple[str, str]:
    """Asynchronously fetch CSV text from a Deckbox set URL or set ID.

    Returns:
      (canonical_export_url, csv_text)

    Raises:
      DeckboxFetchError: the response body was not a CSV export.
    """
    export_url = extract_deckbox_export_url(url_or_id)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; Biblioplex/1.0; +https://github.com/biblioplex)"
        ),
        "Accept": "text/csv,text/plain,*/*",
    }
    if client is not None:
        resp = await client.get(export_url, headers=headers, follow_redirects=True)
        resp.raise_for_status()
        return export_url, _assert_csv_body(resp, export_url)

    async with httpx.AsyncClient(timeout=15.0) as default_client:
        resp = await default_client.get(
            export_url, headers=headers, follow_redirects=True
        )
        resp.raise_for_status()
        return export_url, _assert_csv_body(resp, export_url)


def _parse_money(raw: str | None) -> float | None:
    """`"$1,234.56"` -> `1234.56`. Deckbox writes an empty string, never a
    bare number, when it has no price."""
    if not raw:
        return None
    cleaned = raw.strip().lstrip("$").replace(",", "")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_deckbox_csv(csv_text: str) -> tuple[list[ParsedDeckboxItem], list[str]]:
    """Parse raw Deckbox CSV export text into ParsedDeckboxItems.

    Handles header variations (Count vs Quantity, Name vs Card, Edition vs Set).
    Filters out zero-quantity rows. Aggregates identical card rows.
    """
    warnings: list[str] = []
    lines = [line for line in csv_text.splitlines() if line.strip()]
    if not lines:
        return [], [_FATAL + "CSV file is empty"]

    reader = csv.DictReader(lines)
    if not reader.fieldnames:
        return [], [_FATAL + "CSV header is missing"]

    # Build lower-case header map
    header_map = {fn.strip().lower(): fn for fn in reader.fieldnames if fn}

    count_key = (
        header_map.get("count")
        or header_map.get("quantity")
        or header_map.get("qty")
    )
    name_key = (
        header_map.get("name")
        or header_map.get("card")
        or header_map.get("card name")
    )
    edition_key = (
        header_map.get("edition")
        or header_map.get("set")
        or header_map.get("set name")
    )
    # Real Deckbox exports carry `Edition Code` ("unf", "tla") next to the
    # human-readable `Edition`. Verified 100% populated on a 2597-row
    # inventory export; the `set_code` column existed unread until then.
    set_code_key = (
        header_map.get("edition code")
        or header_map.get("set code")
        or header_map.get("edition_code")
    )
    foil_key = header_map.get("foil")
    price_key = header_map.get("price")

    if not name_key:
        return [], [_FATAL + "CSV missing required 'Name' column"]

    items_map: dict[tuple[str, str | None], ParsedDeckboxItem] = {}

    for row_idx, row in enumerate(reader, start=2):
        raw_name = row.get(name_key, "") if name_key else ""
        if not raw_name or not raw_name.strip():
            continue

        card_name = normalize_card_name(raw_name)

        # Quantity
        qty = 1
        if count_key and row.get(count_key):
            try:
                qty = int(row[count_key].strip())
            except ValueError:
                warnings.append(f"Line {row_idx}: invalid count '{row[count_key]}'")
                qty = 1

        if qty <= 0:
            continue

        # Foil
        foil_qty = 0
        if foil_key and row.get(foil_key):
            val = row[foil_key].strip().lower()
            if val in ("1", "true", "yes", "foil"):
                foil_qty = qty
            elif val.isdigit():
                foil_qty = min(int(val), qty)

        price = _parse_money(row.get(price_key)) if price_key else None
        edition = row.get(edition_key, "").strip() if edition_key else None
        set_name = edition or None
        raw_code = row.get(set_code_key, "").strip() if set_code_key else None
        # Deckbox lower-cases these; store them the way Scryfall does.
        set_code = raw_code.lower() or None if raw_code else None

        key = (card_name.lower(), set_name.lower() if set_name else None)
        if key in items_map:
            existing = items_map[key]
            existing.quantity += qty
            existing.foil_quantity += foil_qty
            # Unit price, not a running total — folded rows differ only by
            # condition, so keep the best copy's price rather than summing.
            if price is not None:
                existing.price_usd = max(existing.price_usd or 0.0, price)
        else:
            items_map[key] = ParsedDeckboxItem(
                card_name=card_name,
                quantity=qty,
                foil_quantity=foil_qty,
                set_name=set_name,
                set_code=set_code,
                price_usd=price,
            )

    return list(items_map.values()), warnings
