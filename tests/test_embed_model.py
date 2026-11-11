"""Tests for the embedding model loader (D5.1, tech_spec.md §8.1).

The loader wraps :class:`sentence_transformers.SentenceTransformer` so the
HF download happens lazily once per process. Tests mock the
``SentenceTransformer`` constructor — no live HuggingFace download is
permitted in CI.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from hypothesis import given
from hypothesis import strategies as st

from biblioplex.config import Settings
from biblioplex.embed.model import load_model


def _make_settings(**overrides: Any) -> Settings:
    base = dict(
        database_url="postgresql+psycopg://placeholder/placeholder",
        hf_embed_model="sentence-transformers/all-MiniLM-L6-v2",
        hf_embed_dim=384,
        hf_embed_device="cpu",
        embed_batch=256,
        bulk_source_type="default_cards",
        web_concurrency=1,
        cors_origins="*",
        log_level="INFO",
    )
    base.update(overrides)
    return Settings.model_construct(**base)


def test_load_model_constructs_with_settings() -> None:
    """`load_model()` calls SentenceTransformer with model name + device from settings."""
    load_model.cache_clear()
    settings = _make_settings(
        hf_embed_model="sentence-transformers/all-MiniLM-L6-v2",
        hf_embed_device="cpu",
    )
    with patch("biblioplex.embed.model.SentenceTransformer") as ctor:
        sentinel = MagicMock(name="SentenceTransformerInstance")
        ctor.return_value = sentinel
        model = load_model(settings)
    assert model is sentinel
    ctor.assert_called_once_with(
        "sentence-transformers/all-MiniLM-L6-v2",
        device="cpu",
        local_files_only=True,
    )


def test_load_model_falls_back_to_download_when_not_cached() -> None:
    """Cache miss (OSError offline) -> retry once allowing the network."""
    load_model.cache_clear()
    sentinel = MagicMock(name="Downloaded")
    with patch("biblioplex.embed.model.SentenceTransformer") as ctor:
        ctor.side_effect = [OSError("not in cache"), sentinel]
        model = load_model(_make_settings())
    assert model is sentinel
    assert ctor.call_args_list[0].kwargs["local_files_only"] is True
    assert "local_files_only" not in ctor.call_args_list[1].kwargs


def test_load_model_caches_singleton() -> None:
    """Second call returns the same instance — model loads exactly once."""
    load_model.cache_clear()
    settings = _make_settings()
    with patch("biblioplex.embed.model.SentenceTransformer") as ctor:
        ctor.return_value = MagicMock()
        first = load_model(settings)
        second = load_model(settings)
    assert first is second
    assert ctor.call_count == 1


# Property test (M5 mandate): the cached singleton holds for any repeat-count >= 1
# of calls with the same settings object.
@given(call_count=st.integers(min_value=1, max_value=20))
def test_load_model_idempotent_under_repeat_calls(call_count: int) -> None:
    """Any number of `load_model(settings)` calls return the same instance."""
    load_model.cache_clear()
    settings = _make_settings()
    with patch("biblioplex.embed.model.SentenceTransformer") as ctor:
        ctor.return_value = MagicMock()
        results = [load_model(settings) for _ in range(call_count)]
    assert all(r is results[0] for r in results)
    assert ctor.call_count == 1


@given(
    model_name=st.sampled_from(
        [
            "sentence-transformers/all-MiniLM-L6-v2",
            "sentence-transformers/paraphrase-MiniLM-L3-v2",
            "intfloat/e5-small-v2",
        ]
    ),
    device=st.sampled_from(["cpu", "cuda"]),
)
def test_load_model_forwards_arbitrary_model_and_device(
    model_name: str, device: str
) -> None:
    """Any (model_name, device) combo from settings flows into the constructor."""
    load_model.cache_clear()
    settings = _make_settings(hf_embed_model=model_name, hf_embed_device=device)
    with patch("biblioplex.embed.model.SentenceTransformer") as ctor:
        ctor.return_value = MagicMock()
        load_model(settings)
    ctor.assert_called_once_with(model_name, device=device, local_files_only=True)
