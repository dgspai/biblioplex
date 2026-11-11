"""Dict-walk projector for ``?projection=`` payloads (D7.2, §7).

Walks each parsed path into the source ``dict`` / ``list`` tree. Missing
segments — key absent, index out of range, type mismatch (walking past a
scalar, or string-indexing a list, or int-indexing a dict) — collapse to
``null`` at the leaf rather than raising. The spec mandates this so
agent clients can pattern-match on field presence with a uniform shape
across the result set.

Multiple paths that share a prefix are merged into the same nested
sub-structure (dict-into-dict, list-into-sparse-list). Lists are
materialized by index — gaps padded with ``None`` so positional
semantics survive (``card_faces.1.name`` without ``.0`` still puts the
name at index 1).
"""

from __future__ import annotations

from typing import Any


def project(row: dict[str, Any], paths: list[list[str | int]]) -> dict[str, Any]:
    """Project ``row`` through ``paths``, returning a nested subset dict.

    :param row: Source card payload (typically a ``raw`` JSONB value).
    :param paths: Parsed projection paths from :func:`parse_projection`.
    :returns: Nested dict mirroring the requested path shape, with
        ``None`` filled in for unreachable leaves.
    """
    out: dict[str, Any] = {}
    for path in paths:
        if not path:
            continue
        value = _walk(row, path)
        _merge(out, path, value)
    return out


def _walk(source: Any, path: list[str | int]) -> Any:
    """Descend ``path`` into ``source``; return the leaf or ``None`` if missing.

    Missing covers:

    * dict missing the string key,
    * list index out of range or wrong type (string key into a list,
      negative / non-int index where an int was expected),
    * walking further into a scalar.
    """
    cursor: Any = source
    for segment in path:
        if isinstance(segment, int):
            if not isinstance(cursor, list) or segment < 0 or segment >= len(cursor):
                return None
            cursor = cursor[segment]
        else:
            if not isinstance(cursor, dict) or segment not in cursor:
                return None
            cursor = cursor[segment]
    return cursor


def _merge(out: dict[str, Any], path: list[str | int], value: Any) -> None:
    """Merge ``value`` into ``out`` at the position described by ``path``.

    Builds intermediate dicts/lists on demand. When two paths share a
    prefix, sub-results are stitched together (e.g. ``card_faces.0.name``
    + ``card_faces.0.oracle_text`` end up under the same face dict).
    """
    # The first segment is always a string (top-level whitelist guarantees
    # it). After that, segment types choose the container shape.
    parent: Any = out
    for i, segment in enumerate(path):
        is_last = i == len(path) - 1
        next_segment = path[i + 1] if not is_last else None
        if isinstance(segment, int):
            # D10.12: replaces `assert` (silently drops under python -O).
            if not isinstance(parent, list):
                raise TypeError("container shape mismatch in projector merge (expected list)")
            _pad_list(parent, segment + 1)
            if is_last:
                parent[segment] = value
            else:
                child = parent[segment]
                child = _ensure_container(child, next_segment)
                parent[segment] = child
                parent = child
        else:
            if not isinstance(parent, dict):
                raise TypeError("container shape mismatch in projector merge (expected dict)")
            if is_last:
                parent[segment] = value
            else:
                child = parent.get(segment)
                child = _ensure_container(child, next_segment)
                parent[segment] = child
                parent = child


def _ensure_container(existing: Any, next_segment: str | int | None) -> Any:
    """Pick or reuse a container shape based on the next path segment.

    If a previous path already materialized a dict at this point and the
    next segment is an int, keep the dict — the type-mismatch case is
    pathological (mixed int/string indexing at the same level) and the
    spec doesn't promise sane behavior for it. Standard usage threads
    consistent path shapes through the same merge.
    """
    if isinstance(next_segment, int):
        return existing if isinstance(existing, list) else []
    return existing if isinstance(existing, dict) else {}


def _pad_list(lst: list[Any], length: int) -> None:
    """Grow ``lst`` to at least ``length``, filling new slots with ``None``."""
    if len(lst) < length:
        lst.extend([None] * (length - len(lst)))
