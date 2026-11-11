"""End-to-end deck eval — template-classifier-plan.md Step 4.

For each ``data/reference_decks/*.txt`` (one card name per line, first
line is the commander), classify every card via ``classify_by_rules``,
sum predicted counts per category (multilabel: a card scoring >= 0.5
in two labels counts in both), and report::

    template_error(deck) = sum |target_c - predicted_c| over 5 cats

Targets: ``{ramp: 12, card_advantage: 12, targeted_disrupt: 12,
mass_disrupt: 6, plan: 30}``. Lands tracked separately (target 38)
but excluded from ``template_error`` since the classifier doesn't
score them.

Run inside the api container::

    docker exec biblioplex-api-1 python scripts/eval_template.py

ponytail: stdlib only (csv-free, pathlib, asyncio already in eval_rules).
Single SQL roundtrip per deck. Cards missing from DB are reported and
skipped — don't silently inflate ``plan``.
"""

from __future__ import annotations

import asyncio
import statistics
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from biblioplex.classify.rules import LABELS, classify_by_rules
from biblioplex.config import Settings

DECKS_DIR = Path(__file__).resolve().parent.parent / "data" / "reference_decks"
THRESHOLD = 0.5
TARGETS = {
    "ramp": 12,
    "card_advantage": 12,
    "targeted_disrupt": 12,
    "mass_disrupt": 6,
    "plan": 30,
}
LAND_TARGET = 38

SQL = """
SELECT name, oracle_text, card_faces, type_line
FROM cards
WHERE name = ANY(:names)
"""


def _is_land(type_line: str | None, card_faces) -> bool:
    if type_line and "Land" in type_line:
        return True
    if isinstance(card_faces, list):
        for f in card_faces:
            if isinstance(f, dict) and "Land" in (f.get("type_line") or ""):
                return True
    return False


def _load_deck(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip()
    ]


async def main() -> None:
    deck_files = sorted(DECKS_DIR.glob("*.txt"))
    if not deck_files:
        print(f"no decks in {DECKS_DIR}", file=sys.stderr)
        sys.exit(1)

    all_names: set[str] = set()
    decks: dict[str, list[str]] = {}
    for p in deck_files:
        names = _load_deck(p)
        decks[p.stem] = names
        all_names.update(names)

    s = Settings()
    engine = create_async_engine(s.database_url)
    try:
        async with engine.begin() as conn:
            db = {
                row.name: row
                for row in (
                    await conn.execute(text(SQL), {"names": list(all_names)})
                ).all()
            }
    finally:
        await engine.dispose()

    missing_global = sorted(set(all_names) - set(db.keys()))
    if missing_global:
        print(
            f"[warn] {len(missing_global)} cards missing from DB "
            f"(first 5: {missing_global[:5]})",
            file=sys.stderr,
        )

    errors: list[int] = []
    header = (
        f"{'deck':<32} "
        + " ".join(f"{lab[:6]:>6}" for lab in LABELS)
        + f" {'land':>6} {'err':>6}"
    )
    target_row = (
        f"{'(target)':<32} "
        + " ".join(f"{TARGETS[lab]:>6}" for lab in LABELS)
        + f" {LAND_TARGET:>6} {'':>6}"
    )
    print(header)
    print(target_row)
    print("-" * len(header))

    for stem, names in decks.items():
        counts = {lab: 0 for lab in LABELS}
        lands = 0
        for n in names:
            row = db.get(n)
            if row is None:
                continue
            if _is_land(row.type_line, row.card_faces):
                lands += 1
                # ponytail: R26 says MDFC land also gets spell label;
                # skipping for first cut (rare; bias adds at most ~1
                # CA/ramp per deck for MDFCs like Sea Gate Restoration).
                continue
            pred = classify_by_rules({
                "oracle_text": row.oracle_text,
                "card_faces": row.card_faces,
            })
            for lab in LABELS:
                if pred[lab] >= THRESHOLD:
                    counts[lab] += 1
        err = sum(abs(TARGETS[lab] - counts[lab]) for lab in LABELS)
        errors.append(err)
        print(
            f"{stem[:32]:<32} "
            + " ".join(f"{counts[lab]:>6}" for lab in LABELS)
            + f" {lands:>6} {err:>6}"
        )

    print("-" * len(header))
    print(f"mean template_error:   {statistics.mean(errors):.2f}")
    print(f"median template_error: {statistics.median(errors):.2f}")
    print(f"ship bar: <= 6   kill bar: >= 15")


if __name__ == "__main__":
    asyncio.run(main())
