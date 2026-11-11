"""Scryfall ``/catalog/*`` snapshots.

Catalogs are plain string arrays Scryfall publishes as canonical token
whitelists (https://scryfall.com/docs/api/catalogs). We fetch the three
keyword catalogs once at app startup and cache them in-process so the
DSL compiler can turn ``is:frenchvanilla`` into a tokenised whitelist
check instead of the lossy "no punctuation in ``oracle_text``" heuristic
that was shipped while §5.1 drift was open.

The fetch is best-effort. If Scryfall is unreachable on boot, the
compiler falls back to the legacy heuristic so search keeps working.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

import httpx

from biblioplex import SCRYFALL_HEADERS

_KEYWORD_ENDPOINTS: tuple[str, ...] = (
    "https://api.scryfall.com/catalog/keyword-abilities",
    "https://api.scryfall.com/catalog/keyword-actions",
    "https://api.scryfall.com/catalog/ability-words",
)

_log = logging.getLogger(__name__)

_keywords: frozenset[str] | None = None
_frenchvanilla_regex: str | None = None

# D15.16: on-disk cache for the boot-time keyword fetch. Scryfall's
# catalogs change on the scale of set releases (~monthly), so a 7-day
# TTL skips the boot-time round-trip on every redeploy without letting
# a stale snapshot drift past a real refresh cadence.
_CACHE_TTL_S: float = 7 * 24 * 3600.0


def _cache_path() -> Path:
    """Return the on-disk cache path, honouring `XDG_CACHE_HOME`."""
    root = os.environ.get("BIBLIOPLEX_CACHE_DIR") or os.environ.get("XDG_CACHE_HOME")
    base = Path(root) if root else Path.home() / ".cache"
    return base / "biblioplex" / "scryfall-keywords.json"


def _load_disk_cache(path: Path, ttl_s: float) -> frozenset[str] | None:
    """Read the cached keyword set if the file exists and is within TTL."""
    try:
        stat = path.stat()
    except OSError:
        return None
    age = time.time() - stat.st_mtime
    if age > ttl_s:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning("scryfall cache read failed at %s: %s", path, exc)
        return None
    if not isinstance(payload, list):
        return None
    return frozenset(str(x) for x in payload if isinstance(x, str))


def _write_disk_cache(path: Path, kws: frozenset[str]) -> None:
    """Best-effort atomic write; failure is logged and swallowed."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(sorted(kws)), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        _log.warning("scryfall cache write failed at %s: %s", path, exc)


async def fetch_keyword_set(client: httpx.AsyncClient) -> frozenset[str]:
    """Return the union of ``keyword-abilities`` ∪ ``keyword-actions`` ∪ ``ability-words``.

    Lowercased. Raises :class:`httpx.HTTPError` on transport failure or
    :class:`ValueError` on a malformed catalog response.

    D15.16: warmed from the on-disk cache when fresh (7-day TTL); the
    HTTP client is only invoked on a cache miss / stale entry. Failure
    modes stay unchanged — a stale cache falls through to a live fetch,
    a live fetch failure still bubbles the exception to lifespan's
    best-effort handler.
    """
    cache = _cache_path()
    cached = _load_disk_cache(cache, _CACHE_TTL_S)
    if cached is not None:
        _log.info("scryfall keyword catalog served from cache (%d entries)", len(cached))
        return cached
    items: set[str] = set()
    for url in _KEYWORD_ENDPOINTS:
        r = await client.get(url, headers=SCRYFALL_HEADERS, timeout=5.0)
        r.raise_for_status()
        payload = r.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise ValueError(f"{url}: missing 'data' array")
        for kw in data:
            if isinstance(kw, str) and kw.strip():
                items.add(kw.strip().lower())
    result = frozenset(items)
    _write_disk_cache(cache, result)
    return result


def set_keyword_set(kws: frozenset[str]) -> None:
    """Replace the in-process keyword cache and rebuild derived regexes."""
    global _keywords, _frenchvanilla_regex
    _keywords = kws
    _frenchvanilla_regex = _build_frenchvanilla_regex(kws) if kws else None


def get_keyword_set() -> frozenset[str] | None:
    """Return the cached keyword set, or ``None`` if never populated."""
    return _keywords


def get_frenchvanilla_regex() -> str | None:
    """POSIX/ARE regex matching a keyword-only oracle text, or ``None``.

    Callers MUST strip parenthesised reminder text from ``oracle_text``
    before applying this pattern — do it in SQL with
    ``regexp_replace(oracle_text, '\\([^)]*\\)', '', 'g')``. The regex
    anchors against the whole stripped text and accepts a
    ``[,;.\\n]``-separated list of tokens; each token is a known keyword
    optionally followed by a numeric or ``X`` argument
    (``Annihilator 1``, ``Bushido X``).
    """
    return _frenchvanilla_regex


def _build_frenchvanilla_regex(kws: frozenset[str]) -> str:
    # Longest-first so multi-word keywords ("First strike") bind before
    # any shorter prefix that shares a head.
    kw_alt = "|".join(re.escape(k) for k in sorted(kws, key=lambda s: (-len(s), s)))
    token = rf"(?:{kw_alt})(?:\s+(?:[0-9]+|X))?"
    return rf"^\s*{token}(?:\s*[,;.\n]\s*{token})*\s*\.?\s*$"
