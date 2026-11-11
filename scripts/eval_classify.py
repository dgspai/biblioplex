"""Zero-shot multilabel classify — embed 4 short label probes, cosine
against every card embedding, sweep τ per label, score against Scryfall
oracle_tags as gold.

Run inside the api container so the SentenceTransformer cache is hot:
    docker exec biblioplex-api-1 python scripts/eval_classify.py

ponytail: one script, no sklearn, no train framework. Numpy + pgvector
cosine. Threshold sweep is 5 lines.
"""

from __future__ import annotations

import asyncio

import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from biblioplex.config import Settings
from biblioplex.embed.model import load_model

LABELS = ["card-advantage", "ramp", "targeted-disrupt", "mass-disrupt"]

OTAGS: dict[str, list[str]] = {
    "card-advantage":   ["card-advantage"],
    "ramp":             ["ramp", "mana ramp", "acceleration"],
    "targeted-disrupt": ["spot-removal"],
    "mass-disrupt":     ["sweeper", "boardwipe", "wipe", "mass removal", "wrath of god"],
}

PROBES: dict[str, str] = {
    "card-advantage":   "draw cards card advantage",
    "ramp":             "add mana search your library for a basic land",
    "targeted-disrupt": "destroy target creature exile target creature",
    "mass-disrupt":     "destroy all creatures exile all creatures",
}

SQL = """
WITH lbl AS (
  SELECT oracle_id,
    bool_or(tag = ANY(:ca))   AS g_ca,
    bool_or(tag = ANY(:rmp))  AS g_ramp,
    bool_or(tag = ANY(:trg))  AS g_targ,
    bool_or(tag = ANY(:mss))  AS g_mass
  FROM oracle_tags GROUP BY oracle_id
)
SELECT c.id, c.name,
  1 - (c.embedding <=> CAST(:v_ca   AS vector)) AS s_ca,
  1 - (c.embedding <=> CAST(:v_ramp AS vector)) AS s_ramp,
  1 - (c.embedding <=> CAST(:v_targ AS vector)) AS s_targ,
  1 - (c.embedding <=> CAST(:v_mass AS vector)) AS s_mass,
  COALESCE(l.g_ca,   false) AS g_ca,
  COALESCE(l.g_ramp, false) AS g_ramp,
  COALESCE(l.g_targ, false) AS g_targ,
  COALESCE(l.g_mass, false) AS g_mass
FROM cards c LEFT JOIN lbl l ON l.oracle_id = c.oracle_id
WHERE c.embedding IS NOT NULL
"""


def vec_lit(v) -> str:
    return "[" + ",".join(repr(float(x)) for x in v) + "]"


def prf1(pred: np.ndarray, gold: np.ndarray) -> tuple[float, float, float, int]:
    tp = int((pred & gold).sum())
    fp = int((pred & ~gold).sum())
    fn = int((~pred & gold).sum())
    p = tp / (tp + fp) if (tp + fp) else 0.0
    r = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * p * r) / (p + r) if (p + r) else 0.0
    return p, r, f1, int(gold.sum())


async def main() -> None:
    s = Settings()
    print(f"# Zero-shot classify — model={s.hf_embed_model}\n")

    model = load_model(s)
    probe_vecs = model.encode(
        [PROBES[lab] for lab in LABELS], normalize_embeddings=True
    )

    params = {
        "ca": OTAGS["card-advantage"], "rmp": OTAGS["ramp"],
        "trg": OTAGS["targeted-disrupt"], "mss": OTAGS["mass-disrupt"],
        "v_ca":   vec_lit(probe_vecs[0]), "v_ramp": vec_lit(probe_vecs[1]),
        "v_targ": vec_lit(probe_vecs[2]), "v_mass": vec_lit(probe_vecs[3]),
    }

    engine = create_async_engine(s.database_url)
    try:
        async with engine.begin() as conn:
            rows = (await conn.execute(text(SQL), params)).all()
    finally:
        await engine.dispose()

    n = len(rows)
    names = np.array([r.name for r in rows])
    scores = np.array([[r.s_ca, r.s_ramp, r.s_targ, r.s_mass] for r in rows])
    gold = np.array(
        [[r.g_ca, r.g_ramp, r.g_targ, r.g_mass] for r in rows], dtype=bool
    )

    rng = np.random.default_rng(42)
    idx = rng.permutation(n)
    cut = int(0.8 * n)
    train, test = idx[:cut], idx[cut:]

    print(f"cards: {n}  train: {len(train)}  test: {len(test)}")
    print(f"gold support (full set): " + ", ".join(
        f"{lab}={int(gold[:, i].sum())}" for i, lab in enumerate(LABELS)
    ) + "\n")

    taus = np.linspace(0.05, 0.95, 91)
    print(f"  {'label':<18} {'τ*':<5} {'P':<6} {'R':<6} {'F1':<6} {'support':<8}")
    print(f"  {'-'*18} {'-'*5} {'-'*6} {'-'*6} {'-'*6} {'-'*8}")

    chosen_tau: dict[str, float] = {}
    f1s: list[float] = []
    for i, lab in enumerate(LABELS):
        y_tr, s_tr = gold[train, i], scores[train, i]
        f1_per_tau = [prf1(s_tr >= t, y_tr)[2] for t in taus]
        tau = float(taus[int(np.argmax(f1_per_tau))])
        chosen_tau[lab] = tau
        p, r, f1, sup = prf1(scores[test, i] >= tau, gold[test, i])
        f1s.append(f1)
        print(f"  {lab:<18} {tau:<5.2f} {p:<6.3f} {r:<6.3f} {f1:<6.3f} {sup:<8}")

    print(f"\n  macro-F1: {sum(f1s) / len(f1s):.3f}")

    # 'plan' = none-of-the-above: predict no label crosses τ.
    pred_any = np.zeros(n, dtype=bool)
    for i, lab in enumerate(LABELS):
        pred_any |= scores[:, i] >= chosen_tau[lab]
    gold_plan = ~gold.any(axis=1)
    pred_plan = ~pred_any
    p, r, f1, sup = prf1(pred_plan[test], gold_plan[test])
    print(f"  plan (residue)     —     {p:<6.3f} {r:<6.3f} {f1:<6.3f} {sup}")

    # Top-5 FP and FN per label, eyeball reality check.
    print("\n# Top-5 false positives (highest score, gold=False)")
    for i, lab in enumerate(LABELS):
        mask = ~gold[:, i]
        order = np.argsort(-scores[:, i])
        top = [j for j in order if mask[j]][:5]
        print(f"  {lab}: " + ", ".join(
            f"{names[j]!r}({scores[j, i]:.2f})" for j in top
        ))

    print("\n# Top-5 false negatives (lowest score, gold=True)")
    for i, lab in enumerate(LABELS):
        mask = gold[:, i]
        order = np.argsort(scores[:, i])
        top = [j for j in order if mask[j]][:5]
        print(f"  {lab}: " + ", ".join(
            f"{names[j]!r}({scores[j, i]:.2f})" for j in top
        ))


if __name__ == "__main__":
    asyncio.run(main())
