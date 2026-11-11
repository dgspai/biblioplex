"""Parser for the ``?projection=`` query string (D7.1, §7).

Input grammar (informal):

    projection := path ("," path)*
    path       := segment ("." segment)*
    segment    := non-empty string

Output: ``list[list[str | int]]``. Digit-only segments are coerced to
``int`` so the downstream projector can index into JSON arrays
(``card_faces.0.name`` → ``["card_faces", 0, "name"]``).

Validation, per spec §7:

* Top-level segment (path[0]) must appear in
  :data:`biblioplex.projection.whitelist.TOP_LEVEL_FIELDS`.
* Depth cap = 6 segments per path. Deeper → reject.
* No empty segments (``foo..bar``, trailing/leading dot, trailing comma).
* No empty input.

All rejections raise :exc:`ProjectionError` with a short human-readable
message. Routers translate that to the 422
``invalid_projection`` envelope (§7, D7.4).
"""

from __future__ import annotations

from biblioplex.projection.whitelist import TOP_LEVEL_FIELDS

# §7 spec line: "Path depth cap: 6 segments."
MAX_DEPTH = 6


class ProjectionError(ValueError):
    """Raised for any invalid ``?projection=`` input.

    Subclasses :class:`ValueError` so existing ``except (ValueError, ...)``
    handlers in the router layer still catch it, but routers should
    pattern-match this specific class to emit the
    ``invalid_projection`` error code.
    """


def parse_projection(s: str) -> list[list[str | int]]:
    """Parse a comma-separated projection string into path lists.

    :param s: Raw ``?projection=`` query value.
    :returns: List of paths; each path is a list of ``str`` / ``int``
        segments (ints where the source segment was a digit string).
    :raises ProjectionError: On any structural or whitelist violation.

    Examples::

        parse_projection("name") == [["name"]]
        parse_projection("card_faces.0.name") == [["card_faces", 0, "name"]]
        parse_projection("name,prices.usd") == [
            ["name"], ["prices", "usd"]
        ]
    """
    if not s or not s.strip():
        raise ProjectionError("projection must not be empty")

    paths: list[list[str | int]] = []
    for raw_path in s.split(","):
        if not raw_path:
            raise ProjectionError("empty projection path")
        segments_raw = raw_path.split(".")
        if len(segments_raw) > MAX_DEPTH:
            raise ProjectionError(
                f"projection path exceeds depth cap of {MAX_DEPTH}: {raw_path!r}"
            )
        segments: list[str | int] = []
        for seg in segments_raw:
            if not seg:
                raise ProjectionError(f"empty segment in projection path {raw_path!r}")
            # Digit-only segment → array index. Negative indices are not
            # supported (Scryfall's path conventions only ever use 0+).
            # Leading zeros ("00", "01") stay as strings — coercing would
            # lose the original text and break roundtrip semantics.
            is_index = seg.isdigit() and (seg == "0" or not seg.startswith("0"))
            segments.append(int(seg) if is_index else seg)
        head = segments[0]
        # Top-level whitelist check. Integer top-level segments are never
        # valid: every Scryfall top-level field is a string identifier.
        if not isinstance(head, str) or head not in TOP_LEVEL_FIELDS:
            raise ProjectionError(f"unknown projection field: {head!r}")
        paths.append(segments)
    return paths
