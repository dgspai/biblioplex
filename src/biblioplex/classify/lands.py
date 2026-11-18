"""Land profile: which colours a land makes, how it enters, and what job it does.

Orthogonal to the five slot roles in ``rules.py``; lands keep their roles and
gain this profile on top.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

_COLOR_BUCKETS = {1: "mono", 2: "dual", 3: "tri", 4: "four", 5: "five"}
_REMINDER = re.compile(r"\([^)\n]*\)")
_ENTERS_TAPPED = re.compile(r"enters (the battlefield )?tapped", re.I)
_CONDITION = re.compile(r"\bunless\b|\bif you\b|\bif it\b|you may pay|if you don't", re.I)
_CREATURE_LAND = re.compile(r"\bbecomes? an? [^.\n]{0,80}\bcreature\b", re.I)
_SAC_OUTLET = re.compile(
    r"sacrifice (a|an|another) (creature|artifact|permanent)\b[^:\n]{0,40}:", re.I
)
# Activated (cost-colon) dig only: a Temple's ETB scry is not a job.
_SELECTION = re.compile(
    r"^[^\n]*\{[^}\n]+\}[^:\n]*:(?!\s*add\b)[^\n]*\b(look at the top|scry \d|surveil \d)",
    re.I | re.M,
)
# An ability whose effect starts with the search, or the Family lands' ETB sacrifice.
_FETCH = re.compile(
    r"(:\s*|sacrifice it\. when you do,\s*)search your library for [^.\n]*?"
    r"\b(land|plains|island|swamp|mountain|forest) cards?\b",
    re.I,
)
_ROLES = ("ramp", "card_advantage", "targeted_disrupt", "mass_disrupt")


def _is_land(type_line: Any) -> bool:
    return isinstance(type_line, str) and re.search(r"\bLand\b", type_line) is not None


def _land_text(card: Mapping[str, Any]) -> tuple[str, bool] | None:
    """Rules text of the land face, and whether the land is the front face."""
    faces = card.get("card_faces")
    if isinstance(faces, list) and faces:
        for i, face in enumerate(faces):
            if isinstance(face, dict) and _is_land(face.get("type_line")):
                return str(face.get("oracle_text") or ""), i == 0
        return None
    if _is_land(str(card.get("type_line") or "").split(" // ")[0]):
        return str(card.get("oracle_text") or ""), True
    return None


def land_profile(card: Mapping[str, Any], labels: Iterable[str]) -> dict[str, Any] | None:
    """``{colors, etb, utility}`` for a land; ``None`` for anything else."""
    found = _land_text(card)
    if found is None:
        return None
    raw_text, front = found
    text = _REMINDER.sub("", raw_text)

    produced = card.get("produced_mana")
    colours = {c for c in produced or [] if c in "WUBRG"}
    colors = "none" if not produced else _COLOR_BUCKETS.get(len(colours), "colorless")

    tapped = next((s for s in re.split(r"(?<=\.)\s", text) if _ENTERS_TAPPED.search(s)), None)
    etb = "untapped" if tapped is None else "conditional" if _CONDITION.search(tapped) else "tapped"

    utility = [lab for lab in _ROLES if front and lab in set(labels)]
    for kind, pat in (
        ("creature_land", _CREATURE_LAND),
        ("sac_outlet", _SAC_OUTLET),
        ("selection", _SELECTION),
        ("fetch", _FETCH),
    ):
        if pat.search(text):
            utility.append(kind)
    return {"colors": colors, "etb": etb, "utility": utility}
