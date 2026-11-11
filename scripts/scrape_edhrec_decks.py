"""Scrape EDHRec average-deck exports into data/reference_decks/.

Pulls 10 commanders across color identities + archetypes from
``https://json.edhrec.com/pages/average-decks/<slug>.json`` and writes
``data/reference_decks/<slug>.txt`` (one card name per line, commander
first).

Run from host:

    python scripts/scrape_edhrec_decks.py

Rate-limited to one request per second; total ~10 seconds for the
default list. Re-runs overwrite the files.

ponytail: httpx (already a dep) + stdlib. No scraper framework.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import httpx

# Basics under "Basics" cardlist arrive collapsed: label="29 Mountains",
# name="Mountain". Pull the leading integer off the label and expand.
_QTY = re.compile(r"^\s*(\d+)\b")

URL = "https://json.edhrec.com/pages/average-decks/{slug}.json"
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "reference_decks"
DELAY_SEC = 1.0

# (slug, commander-name) — slug must match EDHRec URL exactly; commander
# name is written as the first line of the deck file since EDHRec's
# cardlists exclude the commander.
COMMANDERS: list[tuple[str, str]] = [
    ("atraxa-praetors-voice",         "Atraxa, Praetors' Voice"),
    ("edgar-markov",                  "Edgar Markov"),
    ("kenrith-the-returned-king",     "Kenrith, the Returned King"),
    ("krenko-mob-boss",               "Krenko, Mob Boss"),
    ("the-ur-dragon",                 "The Ur-Dragon"),
    ("yuriko-the-tigers-shadow",      "Yuriko, the Tiger's Shadow"),
    ("korvold-fae-cursed-king",       "Korvold, Fae-Cursed King"),
    ("muldrotha-the-gravetide",       "Muldrotha, the Gravetide"),
    ("talrand-sky-summoner",          "Talrand, Sky Summoner"),
    ("karador-ghost-chieftain",       "Karador, Ghost Chieftain"),
]


def fetch_deck(client: httpx.Client, slug: str) -> list[str]:
    r = client.get(URL.format(slug=slug), timeout=15.0)
    r.raise_for_status()
    payload = r.json()
    cardlists = payload["container"]["json_dict"]["cardlists"]
    out: list[str] = []
    for cl in cardlists:
        for cv in cl.get("cardviews", []):
            name = cv["name"]
            m = _QTY.match(cv.get("label") or "")
            qty = int(m.group(1)) if m else 1
            out.extend([name] * qty)
    return out


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with httpx.Client(headers={"Accept": "application/json"}) as client:
        for i, (slug, commander) in enumerate(COMMANDERS):
            if i:
                time.sleep(DELAY_SEC)
            try:
                cards = fetch_deck(client, slug)
            except httpx.HTTPError as exc:
                print(f"[skip] {slug}: {exc}", file=sys.stderr)
                continue
            out = OUT_DIR / f"{slug}.txt"
            with out.open("w") as f:
                f.write(commander + "\n")
                f.write("\n".join(cards) + "\n")
            print(
                f"[ok] {slug}: {len(cards)} cards + commander → {out.name}",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
