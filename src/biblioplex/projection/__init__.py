"""Server-side projection of card payloads per tech_spec.md §7.

Public surface:

* :func:`parse_projection` — turn a ``?projection=`` query string into a
  list of dotted-path segment lists.
* :func:`project` — walk a card ``dict`` through a parsed projection,
  emitting ``null`` on any missing segment (spec-mandated shape stability
  for agent clients).
* :exc:`ProjectionError` — single error type raised by the parser for
  every flavor of invalid input. Routers convert this to a 422 with the
  Scryfall-shaped ``invalid_projection`` envelope (§7, D7.4).
"""

from __future__ import annotations

from biblioplex.projection.parser import ProjectionError, parse_projection
from biblioplex.projection.projector import project

__all__ = ["ProjectionError", "parse_projection", "project"]
