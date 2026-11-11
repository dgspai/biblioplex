"""Grade ``classify_by_rules`` against ``data/labels_gold.csv``.

Run inside the api container so the DB connection lives:

    docker exec biblioplex-api-1 python scripts/eval_rules.py

Reports per-label P/R/F1 + macro-F1, split by gold source
(``strawman`` vs ``otag_sample``) so the noisy otag rows don't drown
out the hand-curated signal. Strawman F1 is the accept-bar number per
template-classifier-plan.md Step 2 (≥ 0.75 ships, < 0.60 kills).

ponytail: stdlib csv. No sklearn. Threshold fixed at 0.5 (binary
multilabel) — the rules layer emits {0.0, 0.7, 0.8, 0.85, 0.9, 0.95,
1.0}, so the 0.5 cut is the same as "did any pattern fire above the
weak bar".
"""

from __future__ import annotations

import asyncio
import csv
import sys
from collections import defaultdict
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from biblioplex.classify.rules import LABELS, classify_by_rules
from biblioplex.config import Settings

GOLD_PATH = Path(__file__).resolve().parent.parent / "data" / "labels_gold.csv"
THRESHOLD = 0.5

SQL = """
SELECT name, oracle_text, card_faces
FROM cards
WHERE name = ANY(:names)
"""


def _prf1(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    p = tp / (tp + fp) if (tp + fp) else 0.0
    r = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * p * r) / (p + r) if (p + r) else 0.0
    return p, r, f1


async def main() -> None:
    rows: list[dict[str, str]] = []
    with GOLD_PATH.open(newline="") as f:
        rows = list(csv.DictReader(f))

    names = [r["name"] for r in rows]
    s = Settings()
    engine = create_async_engine(s.database_url)
    try:
        async with engine.begin() as conn:
            db = {
                row.name: row
                for row in (
                    await conn.execute(text(SQL), {"names": names})
                ).all()
            }
    finally:
        await engine.dispose()

    missing = [n for n in names if n not in db]
    if missing:
        print(
            f"[warn] {len(missing)} gold cards missing from DB "
            f"(first 5: {missing[:5]})",
            file=sys.stderr,
        )

    # per-source tally: {source: {label: [tp, fp, fn]}}
    tally: dict[str, dict[str, list[int]]] = defaultdict(
        lambda: {lab: [0, 0, 0] for lab in LABELS}
    )

    for r in rows:
        if r["name"] not in db:
            continue
        card = db[r["name"]]
        pred = classify_by_rules({
            "oracle_text": card.oracle_text,
            "card_faces": card.card_faces,
        })
        for lab in LABELS:
            y = r[lab] == "1"
            yhat = pred[lab] >= THRESHOLD
            t = tally[r["source"]][lab]
            if yhat and y:
                t[0] += 1
            elif yhat and not y:
                t[1] += 1
            elif not yhat and y:
                t[2] += 1

    for source in ("strawman", "otag_sample"):
        if source not in tally:
            continue
        print(f"\n# {source}")
        print(
            f"  {'label':<18} {'P':<6} {'R':<6} {'F1':<6} "
            f"{'tp':<5} {'fp':<5} {'fn':<5}"
        )
        f1s: list[float] = []
        for lab in LABELS:
            tp, fp, fn = tally[source][lab]
            p, r, f1 = _prf1(tp, fp, fn)
            f1s.append(f1)
            print(
                f"  {lab:<18} {p:<6.3f} {r:<6.3f} {f1:<6.3f} "
                f"{tp:<5} {fp:<5} {fn:<5}"
            )
        print(f"  macro-F1: {sum(f1s) / len(f1s):.3f}")


if __name__ == "__main__":
    asyncio.run(main())
