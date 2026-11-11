"""Stream parser for the Scryfall default_cards bulk file — §4.2 step 2.

Bug.34 (2026-08-17): the bulk file used to be one top-level JSON array
served as ``*.json`` with ``Content-Encoding: gzip`` (so httpx decompressed
it transparently and ``ijson`` walked the array). Scryfall migrated to JSON
Lines: ``*.jsonl.gz``, ``Content-Type: application/gzip``, and NO
``Content-Encoding`` header — meaning the bytes now arrive still gzipped and
each line is a self-contained card object.

That is strictly simpler to consume: one ``json.loads`` per line, no
incremental-array parser and no file-like adapter for it. ``ijson`` and the
``_AsyncByteReader`` shim it needed are gone from this path.

Decompression is explicit (``zlib`` with ``16 + MAX_WBITS`` for a gzip
wrapper) and incremental, so peak RSS stays at one chunk plus one line
rather than the ~520 MB uncompressed whole.

Drop policy (per spec §4.2 step 2): the seven dropped layouts are tokens,
emblems, schemes, planar cards, vanguards, art-series, and
``double_faced_token`` (the token sub-layout — the real DFC layouts are
``transform`` and ``modal_dfc``). Additionally, Scryfall helper entries
whose ``type_line`` is the literal string ``"Card"`` (e.g. ``Red Mana``,
``Poison Counter``, ``Experience``, substitute / box-topper entries) are
dropped — they have no gameplay role and pollute downstream classifier
and similarity outputs. Everything else passes through.

The parser is layout-agnostic beyond the drop filter — face-flatten,
reversible-card split, and staging-row shaping all live in downstream
modules (D1.3, D1.4, D1.5).
"""

from __future__ import annotations

import io
import json
import zlib
from collections.abc import AsyncIterator, Iterator
from typing import IO, Any

# §4.2 step 2 — extras filtered at ingest. Frozen set for O(1) membership
# and to make the constant obviously immutable to readers.
DROPPED_LAYOUTS: frozenset[str] = frozenset(
    {
        "token",
        "emblem",
        "scheme",
        "planar",
        "vanguard",
        "art_series",
        "double_faced_token",
    }
)


def iter_cards(stream: IO[bytes]) -> Iterator[dict[str, Any]]:
    """Stream-yield kept cards from a Scryfall default_cards JSON byte stream.

    Args:
        stream: Binary file-like positioned at the start of the top-level
            JSON array. Caller manages open/close; ``httpx.stream`` produces
            an ``aiter_bytes()`` source — wrap or pre-buffer before passing
            in if you need sync iteration (the refresh runner D1.6 handles
            the async-to-sync bridge).

    Yields:
        Each card object as a ``dict`` exactly as it appears in the source
        JSON. Layout-flatten and reversible-split are downstream concerns.
    """
    for item in _iter_jsonl_sync(stream):
        if item.get("layout") in DROPPED_LAYOUTS:
            continue
        if item.get("type_line") == "Card":
            # ponytail: Scryfall helper entries ("Red Mana", "Poison Counter",
            # "Experience", token / box-topper substitutes) carry type_line
            # == "Card" with empty or non-rules-text oracle_text. They have
            # no gameplay role and polluted classify FPs (see
            # docs/design/zero-shot-classify-experiment.md §Findings 3).
            continue
        yield item


def _keep(card: dict[str, Any]) -> bool:
    """Shared drop policy so the sync and async paths cannot diverge."""
    if card.get("layout") in DROPPED_LAYOUTS:
        return False
    return card.get("type_line") != "Card"


def _decode_lines(
    chunk: bytes,
    decomp: zlib._Decompress | None,
    buf: bytes,
) -> tuple[list[bytes], bytes]:
    """Feed one wire chunk through gunzip and split off complete lines.

    Returns ``(complete_lines, remainder)``. The remainder is the partial
    trailing line, carried into the next call — a 5 KB card object routinely
    straddles a chunk boundary.
    """
    raw = decomp.decompress(chunk) if decomp is not None else chunk
    if not raw:
        return [], buf
    buf += raw
    *lines, remainder = buf.split(b"\n")
    return lines, remainder


def _parse_line(line: bytes) -> dict[str, Any] | None:
    """One JSONL line -> card dict, or None for blanks / non-objects."""
    line = line.strip()
    if not line:
        return None
    obj = json.loads(line)
    return obj if isinstance(obj, dict) else None


def _iter_jsonl_sync(stream: IO[bytes]) -> Iterator[dict[str, Any]]:
    """Yield every JSON object from a (possibly gzipped) JSONL byte stream."""
    first = stream.read(2)
    # Gzip magic. Callers may hand us either raw or already-decompressed
    # bytes; sniffing keeps both working without a flag to get wrong.
    gzipped = first[:2] == b"\x1f\x8b"
    decomp = zlib.decompressobj(16 + zlib.MAX_WBITS) if gzipped else None
    buf = b""
    chunk = first
    while chunk:
        lines, buf = _decode_lines(chunk, decomp, buf)
        for line in lines:
            obj = _parse_line(line)
            if obj is not None:
                yield obj
        chunk = stream.read(1 << 16)
    obj = _parse_line(buf)
    if obj is not None:
        yield obj


def iter_jsonl_bytes(payload: bytes) -> Iterator[dict[str, Any]]:
    """Yield JSON objects from an in-memory (possibly gzipped) JSONL blob.

    For bulk files small enough to hold whole — oracle-tags is ~6 MB
    gzipped. Card ingest streams instead; see :func:`iter_cards_async`.
    """
    return _iter_jsonl_sync(io.BytesIO(payload))


async def iter_cards_async(
    chunks: AsyncIterator[bytes],
) -> AsyncIterator[dict[str, Any]]:
    """Async twin of :func:`iter_cards` over ``httpx.Response.aiter_bytes()``.

    Applies the same drop policy via :func:`_keep`. Replaces the old
    ``ijson.items_async`` + ``_AsyncByteReader`` pair (Bug.34).
    """
    decomp: zlib._Decompress | None = None
    sniffed = False
    buf = b""
    async for chunk in chunks:
        if not sniffed:
            sniffed = True
            if chunk[:2] == b"\x1f\x8b":
                decomp = zlib.decompressobj(16 + zlib.MAX_WBITS)
        lines, buf = _decode_lines(chunk, decomp, buf)
        for line in lines:
            obj = _parse_line(line)
            if obj is not None and _keep(obj):
                yield obj
    obj = _parse_line(buf)
    if obj is not None and _keep(obj):
        yield obj
