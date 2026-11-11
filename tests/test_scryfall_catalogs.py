"""`biblioplex.scryfall.catalogs` — keyword cache + frenchvanilla regex.

Covers the catalog cache contract (get/set round-trip, derived regex
invalidation) and the regex semantics required by §5.1: keyword-only
oracle text passes, anything with rules text doesn't. Network fetch is
covered indirectly by `test_lifespan_catalogs.py` (mocked httpx)."""

from __future__ import annotations

import re

import pytest

from biblioplex.scryfall import catalogs


@pytest.fixture(autouse=True)
def _reset_cache():
    """Each test runs against a clean module cache."""
    catalogs.set_keyword_set(frozenset())
    yield
    catalogs.set_keyword_set(frozenset())


def test_empty_set_yields_no_regex() -> None:
    catalogs.set_keyword_set(frozenset())
    assert catalogs.get_keyword_set() == frozenset()
    assert catalogs.get_frenchvanilla_regex() is None


def test_populated_set_yields_regex() -> None:
    catalogs.set_keyword_set(frozenset({"flying", "vigilance"}))
    assert catalogs.get_keyword_set() == frozenset({"flying", "vigilance"})
    pat = catalogs.get_frenchvanilla_regex()
    assert pat is not None
    assert "flying" in pat and "vigilance" in pat


@pytest.fixture
def matcher():
    """A re.Pattern over a realistic-ish keyword set."""
    kws = frozenset(
        {
            "flying",
            "vigilance",
            "lifelink",
            "first strike",
            "double strike",
            "trample",
            "haste",
            "deathtouch",
            "annihilator",
            "bushido",
        }
    )
    catalogs.set_keyword_set(kws)
    pat = catalogs.get_frenchvanilla_regex()
    assert pat is not None
    return re.compile(pat, re.IGNORECASE)


def test_matches_single_keyword(matcher) -> None:
    assert matcher.match("Flying")


def test_matches_comma_list(matcher) -> None:
    assert matcher.match("Flying, vigilance, lifelink")


def test_matches_multi_word_keyword(matcher) -> None:
    assert matcher.match("First strike, trample")
    assert matcher.match("Double strike")


def test_matches_keyword_with_numeric_arg(matcher) -> None:
    assert matcher.match("Annihilator 1")
    assert matcher.match("Bushido 2, trample")


def test_rejects_rules_text(matcher) -> None:
    assert not matcher.match(
        "Whenever this creature attacks, draw a card."
    )
    assert not matcher.match("Flying. When this creature dies, gain 3 life.")


def test_rejects_unknown_keyword(matcher) -> None:
    # "menace" is intentionally not in the test set above.
    assert not matcher.match("Flying, menace")


def test_set_keyword_set_replaces_cache() -> None:
    catalogs.set_keyword_set(frozenset({"flying"}))
    first = catalogs.get_frenchvanilla_regex()
    catalogs.set_keyword_set(frozenset({"trample"}))
    second = catalogs.get_frenchvanilla_regex()
    assert first != second
    assert "trample" in second
    assert "flying" not in second


# --- D15.16: on-disk cache ---------------------------------------------------


class _FakeHTTPClient:
    """Counts HTTP fetches so we can assert cache-hit behavior."""

    def __init__(self, keywords: list[str]) -> None:
        self._keywords = keywords
        self.calls: int = 0

    async def get(self, *_args, **_kwargs):
        self.calls += 1

        class _R:
            def __init__(self, payload: dict[str, list[str]]) -> None:
                self._payload = payload

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, list[str]]:
                return self._payload

        return _R({"object": "catalog", "data": self._keywords})


async def test_d15_16_disk_cache_warm_skips_http(tmp_path, monkeypatch) -> None:
    """Second fetch within TTL reads the disk cache — HTTP client not touched."""
    monkeypatch.setenv("BIBLIOPLEX_CACHE_DIR", str(tmp_path))
    client = _FakeHTTPClient(["Flying", "Trample"])
    first = await catalogs.fetch_keyword_set(client)  # type: ignore[arg-type]
    assert client.calls == 3  # three catalog endpoints on cold cache
    second = await catalogs.fetch_keyword_set(client)  # type: ignore[arg-type]
    assert client.calls == 3, "warm cache must not re-fetch"
    assert first == second == frozenset({"flying", "trample"})
    cache_file = tmp_path / "biblioplex" / "scryfall-keywords.json"
    assert cache_file.exists()


async def test_d15_16_stale_disk_cache_refetched(tmp_path, monkeypatch) -> None:
    """Cache older than TTL falls through to a live fetch."""
    monkeypatch.setenv("BIBLIOPLEX_CACHE_DIR", str(tmp_path))
    client = _FakeHTTPClient(["Flying"])
    await catalogs.fetch_keyword_set(client)  # type: ignore[arg-type]
    cache_file = tmp_path / "biblioplex" / "scryfall-keywords.json"
    # Age the mtime past the TTL.
    import os

    stale = os.path.getmtime(cache_file) - (8 * 24 * 3600)
    os.utime(cache_file, (stale, stale))
    await catalogs.fetch_keyword_set(client)  # type: ignore[arg-type]
    assert client.calls == 6  # 3 cold + 3 stale-triggered refetch
