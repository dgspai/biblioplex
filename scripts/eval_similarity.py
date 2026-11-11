"""Similarity eval — measure /cards/:id/similar against known mirror groups.

Run against a live stack: `python scripts/eval_similarity.py`.
Override base URL with BIBLIOPLEX_URL env. Prints per-pair, per-group,
and global stats so each tuning step (embed model, oracle weighting,
trgm tiebreak) can be compared apples-to-apples.

ponytail: stdlib + httpx (already a dep). No framework, no async — this
is a one-shot script, not the hot path.
"""

from __future__ import annotations

import os
import sys

import httpx

BASE_URL = os.environ.get("BIBLIOPLEX_URL", "http://localhost:8000")
TOP_K = 100  # fetch wider than the pass@20 bar so we can record rank for misses
PASS_AT = 20

# Mirror groups: every card in a group should rank every other card high.
# Commander-leaning. Add/remove as the tuning iterations narrow in.
GROUPS: list[list[str]] = [
    # User-supplied
    ["Urza, Chief Artificer", "Urza, Lord High Artificer"],
    ["Etherium Sculptor", "Voyager Quickwelder", "Foundry Inspector", "Cloud Key"],
    ["Rhystic Study", "Smothering Tithe"],
    ["Counterspell", "Cancel", "Negate"],
    # Mirror-rule pairs from the original prompt
    ["Abyssal Persecutor", "Platinum Angel"],
    ["Iron Man, Titan of Innovation", "Repurposing Bay"],
    # Removal
    ["Doom Blade", "Go for the Throat", "Hero's Downfall", "Murder"],
    # Ramp
    ["Cultivate", "Kodama's Reach", "Explosive Vegetation", "Skyshroud Claim"],
    # Card draw
    ["Divination", "Night's Whisper", "Sign in Blood", "Read the Bones"],
    # Cantrip filter
    ["Brainstorm", "Ponder", "Preordain", "Opt"],
    # Bolt-likes
    ["Lightning Bolt", "Lava Spike", "Chain Lightning", "Galvanic Blast"],
    # Wraths
    ["Wrath of God", "Damnation", "Day of Judgment", "Supreme Verdict"],
    # Graveyard hate
    ["Bojuka Bog", "Tormod's Crypt", "Relic of Progenitus", "Soul-Guide Lantern"],
    # Mana doublers
    ["Mana Reflection", "Zendikar Resurgent", "Mirari's Wake"],
    # Modal X-cost artifact creatures
    ["Hangarback Walker", "Walking Ballista"],
    # Uncounterable enablers (utility lands flavor)
    ["Cavern of Souls", "Boseiju, Who Endures"],
]


def resolve(client: httpx.Client, name: str) -> str | None:
    """Look up a card's UUID by exact name. Returns None on 404."""
    r = client.get(f"{BASE_URL}/cards/named", params={"exact": name})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return str(r.json()["id"])


def similar_ids(client: httpx.Client, card_id: str) -> list[tuple[str, float]]:
    """Return [(id, similarity)] of the top-K neighbours, ordered."""
    r = client.get(
        f"{BASE_URL}/cards/{card_id}/similar",
        params={"limit": TOP_K, "projection": "id,similarity"},
    )
    r.raise_for_status()
    return [(str(e["id"]), float(e["similarity"])) for e in r.json()["data"]]


def main() -> int:
    print(f"# Similarity eval — {BASE_URL}  top-K={TOP_K}  pass@={PASS_AT}\n")

    with httpx.Client(timeout=30.0) as client:
        # Resolve every name once; report and skip groups with unknowns.
        resolved: dict[str, str] = {}
        for group in GROUPS:
            for name in group:
                if name in resolved:
                    continue
                cid = resolve(client, name)
                if cid is None:
                    print(f"  [missing] {name!r}")
                else:
                    resolved[name] = cid

        ranks: list[int | None] = []  # None == outside top-K
        sims_when_hit: list[float] = []
        per_group_pass: list[tuple[str, int, int]] = []

        for group in GROUPS:
            ids = [(n, resolved[n]) for n in group if n in resolved]
            if len(ids) < 2:
                continue
            print(f"\n## {' / '.join(n for n, _ in ids)}")

            group_pairs = 0
            group_hits = 0
            for src_name, src_id in ids:
                neighbours = similar_ids(client, src_id)
                index = {cid: (i + 1, sim) for i, (cid, sim) in enumerate(neighbours)}
                for tgt_name, tgt_id in ids:
                    if tgt_id == src_id:
                        continue
                    group_pairs += 1
                    hit = index.get(tgt_id)
                    if hit is None:
                        ranks.append(None)
                        print(f"  {src_name!r:>40} → {tgt_name!r:<40} rank=---  sim=----")
                        continue
                    rank, sim = hit
                    ranks.append(rank)
                    sims_when_hit.append(sim)
                    if rank <= PASS_AT:
                        group_hits += 1
                    print(
                        f"  {src_name!r:>40} → {tgt_name!r:<40} "
                        f"rank={rank:>3}  sim={sim:.3f}"
                    )

            per_group_pass.append((group[0], group_hits, group_pairs))

    print("\n# Summary\n")
    for label, hits, pairs in per_group_pass:
        print(f"  {label!r:>40}  pass@{PASS_AT} {hits}/{pairs}")

    total = len(ranks)
    hits_at_k = sum(1 for r in ranks if r is not None and r <= PASS_AT)
    found = [r for r in ranks if r is not None]
    mean_rank_found = sum(found) / len(found) if found else float("nan")
    mean_sim = sum(sims_when_hit) / len(sims_when_hit) if sims_when_hit else float("nan")

    print(f"\n  total pairs ........ {total}")
    print(f"  pass@{PASS_AT} .............. {hits_at_k}/{total} ({hits_at_k / total:.0%})")
    print(f"  mean rank (found) .. {mean_rank_found:.1f}")
    print(f"  mean sim (found) ... {mean_sim:.3f}")
    print(f"  missing from top-{TOP_K}. {total - len(found)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
