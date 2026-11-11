"""Unit tests for the oracle-tag bulk → row-set expansion.

DB and HTTP are not exercised; we feed a synthetic Scryfall tag graph
to :func:`build_rows` and assert the flattening semantics:

* Direct ``taggings`` produce ``(oracle_id, slug)`` rows.
* Parent tags inherit every descendant tag's taggings.
* ``aliases`` produce additional ``(oracle_id, alias)`` rows.
* Cyclic ``child_ids`` are tolerated, not infinite-looped.
"""

from __future__ import annotations

from biblioplex.ingest.tags import build_rows


def _tag(
    tid: str,
    slug: str,
    *,
    children: list[str] | None = None,
    taggings: list[str] | None = None,
    aliases: list[str] | None = None,
) -> dict[str, object]:
    return {
        "object": "tag",
        "id": tid,
        "slug": slug,
        "type": "oracle",
        "child_ids": children or [],
        "aliases": aliases or [],
        "taggings": [{"oracle_id": oid, "weight": "median"} for oid in (taggings or [])],
    }


def test_leaf_tag_emits_one_row_per_tagging() -> None:
    tags = [_tag("t1", "removal", taggings=["o1", "o2"])]
    rows = build_rows(tags)
    pairs = {(r["oracle_id"], r["tag"]) for r in rows}
    assert pairs == {("o1", "removal"), ("o2", "removal")}


def test_parent_inherits_descendant_taggings() -> None:
    tags = [
        _tag("parent", "removal", children=["c1", "c2"]),
        _tag("c1", "instant-removal", taggings=["o1"]),
        _tag("c2", "sorcery-removal", taggings=["o2"]),
    ]
    rows = build_rows(tags)
    pairs = {(r["oracle_id"], r["tag"]) for r in rows}
    assert ("o1", "removal") in pairs
    assert ("o2", "removal") in pairs
    assert ("o1", "instant-removal") in pairs
    assert ("o2", "sorcery-removal") in pairs


def test_aliases_emit_additional_rows() -> None:
    tags = [_tag("t1", "tutor-creature-giant", taggings=["o1"], aliases=["tutor-giant"])]
    rows = build_rows(tags)
    pairs = {(r["oracle_id"], r["tag"]) for r in rows}
    assert pairs == {("o1", "tutor-creature-giant"), ("o1", "tutor-giant")}


def test_slugs_lowercased() -> None:
    tags = [_tag("t1", "Removal", taggings=["o1"], aliases=["KILL-SPELL"])]
    rows = build_rows(tags)
    tags_set = {r["tag"] for r in rows}
    assert tags_set == {"removal", "kill-spell"}


def test_cycle_does_not_infinite_loop() -> None:
    tags = [
        _tag("a", "a", children=["b"], taggings=["oa"]),
        _tag("b", "b", children=["a"], taggings=["ob"]),
    ]
    rows = build_rows(tags)
    pairs = {(r["oracle_id"], r["tag"]) for r in rows}
    # Each tag at minimum has its own direct tagging.
    assert ("oa", "a") in pairs
    assert ("ob", "b") in pairs


def test_bug30_clear_uses_delete_not_truncate() -> None:
    """Bug.30 — TRUNCATE takes ACCESS EXCLUSIVE and blocks every reader
    (`/similar` target row, `/tags`, DSL `otag:`) for the entire refresh
    txn, which exceeds the 5s read statement_timeout on a 500k-row
    rebuild. DELETE takes ROW EXCLUSIVE; readers keep hitting the
    pre-refresh snapshot under MVCC until commit, then atomic swap."""
    from biblioplex.ingest import tags as tags_module

    sql = tags_module._CLEAR_SQL
    assert "TRUNCATE" not in sql.upper()
    assert sql.strip().upper().startswith("DELETE FROM ORACLE_TAGS")


def test_dedup_across_paths() -> None:
    # Diamond hierarchy: parent -> {c1, c2} -> leaf
    tags = [
        _tag("p", "p", children=["c1", "c2"]),
        _tag("c1", "c1", children=["leaf"]),
        _tag("c2", "c2", children=["leaf"]),
        _tag("leaf", "leaf", taggings=["o1"]),
    ]
    rows = build_rows(tags)
    pairs = [(r["oracle_id"], r["tag"]) for r in rows]
    # `o1` reachable via p->c1->leaf and p->c2->leaf — must dedup.
    assert pairs.count(("o1", "p")) == 1
