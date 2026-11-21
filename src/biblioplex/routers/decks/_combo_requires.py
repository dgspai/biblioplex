"""Template requirements ("1x Persist Creature") checked against a deck.

``REQUIRES_CTE`` intersects the deck with every template's card set once
(hash join over the unnested arrays); ``REQUIRES_LATERAL`` then gives each
``combos co`` row ``req.requires`` (jsonb list) and ``req.missing`` (cards
still needed). Unchecked templates (``oracle_ids IS NULL``) never count as
missing: the combo stays listed, flagged ``checked: false``.
Needs ``:deck_oids`` bound. The deck's matches exclude the combo's named cards.
"""

from __future__ import annotations

from typing import Any

REQUIRES_CTE = (
    "tpl_hits AS ( "
    "    SELECT t.id AS template_id, array_agg(u.oid) AS hits "
    "    FROM combo_templates t "
    "    CROSS JOIN LATERAL unnest(t.oracle_ids) u(oid) "
    "    JOIN unnest(CAST(:deck_oids AS uuid[])) d(oid) ON d.oid = u.oid "
    "    GROUP BY t.id "
    ") "
)

# `owned_sql` adds an `owned` count per template (inventory cards matching it,
# not already in the deck); "NULL" leaves it out.
_LATERAL = (
    "LEFT JOIN LATERAL ( "
    "    SELECT COALESCE(jsonb_agg(jsonb_build_object( "
    "               'name', t.name, 'query', t.scryfall_query, "
    "               'quantity', r.quantity, 'checked', t.oracle_ids IS NOT NULL, "
    "               'in_deck', COALESCE(hv.names, CAST('{{}}' AS text[])), "
    "               'missing', h.missing, 'owned', {owned_sql} "
    "           ) ORDER BY t.id), CAST('[]' AS jsonb)) AS requires, "
    "           COALESCE(sum(h.missing), 0)::int AS missing, "
    "           bool_and(h.missing = 0 OR {owned_sql} >= h.missing) AS all_owned "
    "    FROM combo_requires r "
    "    JOIN combo_templates t ON t.id = r.template_id "
    "    LEFT JOIN tpl_hits th ON th.template_id = t.id "
    "    CROSS JOIN LATERAL ( "
    "        SELECT count(*)::int AS have, array_agg(cd.name ORDER BY cd.name) AS names "
    "        FROM unnest(th.hits) hit(oid) "
    "        JOIN cards cd ON cd.oracle_id = hit.oid "
    "        WHERE NOT (hit.oid = ANY(co.oracle_ids)) "
    "    ) hv "
    "    CROSS JOIN LATERAL (SELECT CASE WHEN t.oracle_ids IS NULL THEN 0 "
    "        ELSE GREATEST(r.quantity - hv.have, 0) END AS missing) h "
    "    WHERE r.combo_id = co.id "
    ") req ON true "
)

REQUIRES_LATERAL = _LATERAL.format(owned_sql="NULL")

_OWNED_SQL = (
    "(SELECT count(DISTINCT i.oracle_id) FROM inventory i "
    " WHERE i.quantity > 0 AND i.oracle_id = ANY(t.oracle_ids) "
    "   AND NOT (i.oracle_id = ANY(CAST(:deck_oids AS uuid[]))))"
)
REQUIRES_LATERAL_OWNED = _LATERAL.format(owned_sql=_OWNED_SQL)


def requires_payload(value: Any) -> list[dict[str, Any]]:
    """``req.requires`` jsonb → response list (psycopg already decodes jsonb)."""
    return [
        {
            "name": r["name"],
            "query": r["query"],
            "quantity": int(r["quantity"]),
            "checked": bool(r["checked"]),
            "in_deck": list(r["in_deck"] or []),
            "missing": int(r["missing"]),
            **({"owned": int(r["owned"])} if r.get("owned") is not None else {}),
        }
        for r in (value or [])
    ]
