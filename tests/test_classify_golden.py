"""Golden set: manual role overrides exported from a live instance (`is:edited`).

Each entry is a card whose labels a human pinned; the rules layer must agree.
Re-export when more overrides accumulate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from biblioplex.classify.rules import LABELS, classify_by_rules

GOLDEN = json.loads(
    (Path(__file__).parent / "fixtures" / "label_overrides.json").read_text()
)


@pytest.mark.parametrize("card", GOLDEN, ids=[c["name"] for c in GOLDEN])
def test_rules_agree_with_manual_override(card: dict) -> None:
    scores = classify_by_rules(card)
    assert [lab for lab in LABELS if scores[lab] >= 0.5] == card["labels"]
