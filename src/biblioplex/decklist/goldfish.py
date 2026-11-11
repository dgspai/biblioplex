"""D14.17 — deterministic solitaire (goldfish) sim.

Companion to :mod:`biblioplex.decklist.mulligan`. The pool builder is
identical (a flat list of ``(name, cmc, is_land)`` tuples). The sim
walks turns and asks the coarsest curve-shape question: given the
mulligan-kept hand, how much mana / how many spells does the deck
develop by turn N?

Naive rules — everything an optimising human would call "wrong" is
intentionally out of scope:

* Mana = count of lands in play. We ignore mana COLORS entirely
  (colored-mana viability is a separate concern; goldfish is about the
  curve). A colorless "goldfish" is the traditional convention.
* Land drop: play *any* land in hand; the greedy pick if multiple.
* Cast: from hand each turn, in cmc DESC order, keep casting until no
  card in hand has ``cmc <= mana_available`` remaining. This
  overestimates spells cast a bit (tap-out play).
* Mulligan: London mulligan on ``keep_land_min <= lands <=
  keep_land_max`` (defaults ``2..5``), bottom nothing. Cap at
  ``max_mulligans`` — if we can't keep one, accept the last one drawn.
* Sample line format: one text row per turn describing lands/hand for
  spot-checking a seeded run.

Non-goals: interaction (counterspells / removal), targeting, mulligan-
to-4 EV, activated abilities. Agents needing that model their own sim.
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass

__all__ = ["GoldfishStats", "run_goldfish"]


@dataclass(frozen=True)
class GoldfishStats:
    """Aggregate stats over ``sims`` seeded runs of the goldfish sim."""

    sims: int
    hand_size: int
    turns: int
    hands_kept: int
    mulligans_taken: int
    avg_mana_by_turn: tuple[float, ...]
    avg_spells_cast_by_turn: tuple[float, ...]
    avg_lands_in_play_by_turn: tuple[float, ...]
    sample_lines: tuple[str, ...]


def _keep_hand(
    hand: list[tuple[str, int | None, bool]],
    *,
    keep_land_min: int,
    keep_land_max: int,
) -> bool:
    n_lands = sum(1 for _n, _c, is_land in hand if is_land)
    return keep_land_min <= n_lands <= keep_land_max


def _one_sim(
    pool: list[tuple[str, int | None, bool]],
    *,
    turns: int,
    hand_size: int,
    rng: random.Random,
    keep_land_min: int,
    keep_land_max: int,
    max_mulligans: int,
) -> tuple[list[int], list[int], list[int], int, list[str]]:
    """Run one seeded solitaire sim.

    Returns (mana_by_turn, spells_by_turn, lands_by_turn, mulligans,
    turn_text_lines).
    """
    # Mulligan loop: keep drawing until we hit a keep OR exhaust cap.
    deck = pool.copy()
    hand: list[tuple[str, int | None, bool]] = []
    mulligans = 0
    for _attempt in range(max_mulligans + 1):
        rng.shuffle(deck)
        hand = deck[:hand_size]
        if _keep_hand(
            hand, keep_land_min=keep_land_min, keep_land_max=keep_land_max
        ):
            break
        mulligans += 1
    library = deck[hand_size:]

    mana_by_turn: list[int] = []
    spells_by_turn: list[int] = []
    lands_in_play_by_turn: list[int] = []
    lines: list[str] = []

    lands_in_play = 0
    for t in range(1, turns + 1):
        # Draw step (skip on turn 1 for player on the play — simplified
        # to always drawing; we don't model play/draw, and one card of
        # noise is invisible in a 500-sim average).
        if library:
            hand.append(library.pop(0))

        # Land drop — one per turn max.
        for i, (_n, _c, is_land) in enumerate(hand):
            if is_land:
                hand.pop(i)
                lands_in_play += 1
                break

        # Spell casts — greedy, biggest first.
        mana_avail = lands_in_play
        spells_cast = 0
        # Sort by cmc DESC, ties broken by pool order (stable).
        spell_idx = sorted(
            [i for i, (_n, c, is_land) in enumerate(hand)
             if not is_land and c is not None],
            key=lambda i: hand[i][1] or 0,
            reverse=True,
        )
        removed: set[int] = set()
        for idx in spell_idx:
            _n, c, _il = hand[idx]
            if c is None:
                continue
            if c <= mana_avail:
                mana_avail -= c
                spells_cast += 1
                removed.add(idx)
        if removed:
            hand = [c for i, c in enumerate(hand) if i not in removed]

        mana_by_turn.append(lands_in_play)
        spells_by_turn.append(spells_cast)
        lands_in_play_by_turn.append(lands_in_play)
        lines.append(
            f"T{t}: lands={lands_in_play} spells_cast={spells_cast} hand={len(hand)}"
        )

    return mana_by_turn, spells_by_turn, lands_in_play_by_turn, mulligans, lines


def run_goldfish(
    pool: list[tuple[str, int | None, bool]],
    *,
    sims: int,
    turns: int,
    hand_size: int,
    rng: random.Random,
    keep_land_min: int = 2,
    keep_land_max: int = 5,
    max_mulligans: int = 3,
) -> GoldfishStats:
    """Run ``sims`` seeded goldfish runs; average per-turn stats.

    Returns :class:`GoldfishStats` with all-zeros / empty tuples if the
    sim can't run (empty pool, non-positive counts, or hand_size >
    len(pool) — a 7-card opening from a 6-card pool is undefined).
    """
    if (
        not pool
        or sims <= 0
        or turns <= 0
        or hand_size <= 0
        or hand_size > len(pool)
    ):
        return GoldfishStats(
            sims=0,
            hand_size=hand_size,
            turns=turns,
            hands_kept=0,
            mulligans_taken=0,
            avg_mana_by_turn=(),
            avg_spells_cast_by_turn=(),
            avg_lands_in_play_by_turn=(),
            sample_lines=(),
        )

    mana_bins: list[list[int]] = [[] for _ in range(turns)]
    spells_bins: list[list[int]] = [[] for _ in range(turns)]
    lands_bins: list[list[int]] = [[] for _ in range(turns)]
    total_mulligans = 0
    sample_lines: list[str] = []
    for s in range(sims):
        mana, spells, lands, muls, lines = _one_sim(
            pool,
            turns=turns,
            hand_size=hand_size,
            rng=rng,
            keep_land_min=keep_land_min,
            keep_land_max=keep_land_max,
            max_mulligans=max_mulligans,
        )
        total_mulligans += muls
        for i in range(turns):
            mana_bins[i].append(mana[i])
            spells_bins[i].append(spells[i])
            lands_bins[i].append(lands[i])
        # Only capture sample_lines from the FIRST sim for
        # spot-checking (deterministic under seed).
        if s == 0:
            sample_lines = lines

    return GoldfishStats(
        sims=sims,
        hand_size=hand_size,
        turns=turns,
        hands_kept=sims,
        mulligans_taken=total_mulligans,
        avg_mana_by_turn=tuple(
            round(statistics.mean(b), 4) for b in mana_bins
        ),
        avg_spells_cast_by_turn=tuple(
            round(statistics.mean(b), 4) for b in spells_bins
        ),
        avg_lands_in_play_by_turn=tuple(
            round(statistics.mean(b), 4) for b in lands_bins
        ),
        sample_lines=tuple(sample_lines),
    )
