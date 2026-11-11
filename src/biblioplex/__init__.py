"""Biblioplex — Scryfall-compatible MTG card search backend."""

from importlib.metadata import PackageNotFoundError, version

# Single source of truth is pyproject.toml; read it back from the installed
# package metadata. An editable install records the version at install time,
# so re-run `make install` after bumping it.
try:
    __version__ = version("biblioplex")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0+unknown"

USER_AGENT = f"biblioplex/{__version__} (+https://github.com/dgspai/biblioplex)"
SCRYFALL_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}
# Per-phase httpx timeout (connect / per-chunk read / write / pool) for the
# multi-hundred-MB bulk downloads. Bounds a stalled socket, not the whole
# transfer — a slow-but-moving stream never trips it. Was `None`, which let
# one dead connection hang `/admin/refresh` forever.
SCRYFALL_BULK_TIMEOUT = 60.0
