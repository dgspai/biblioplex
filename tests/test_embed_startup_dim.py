"""Tests for the startup dim-check (D5.2, tech_spec.md §8.1).

When the FastAPI lifespan boots, we load the embedding model and assert
that its declared dimension matches ``HF_EMBED_DIM``. Mismatch → raise
``RuntimeError`` to fail-fast (caller-visible at boot).

Implementation lives in :func:`biblioplex.embed.model.assert_model_dim`
and is wired through :func:`biblioplex.main.lifespan`. Tests mock the
SentenceTransformer so no HF download is triggered.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given
from hypothesis import strategies as st

from biblioplex.config import Settings
from biblioplex.embed.model import assert_model_dim, load_model


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


def _stub_model(dim: int) -> MagicMock:
    model = MagicMock()
    model.get_sentence_embedding_dimension.return_value = dim
    return model


def test_assert_model_dim_matches_returns_none() -> None:
    """Matching dim → silent success."""
    settings = _make_settings(hf_embed_dim=384)
    model = _stub_model(384)
    assert assert_model_dim(model, settings) is None


def test_assert_model_dim_mismatch_raises() -> None:
    """Mismatched dim → RuntimeError mentioning both numbers."""
    settings = _make_settings(hf_embed_dim=384)
    model = _stub_model(768)
    with pytest.raises(RuntimeError, match=r"dim mismatch.*768.*384"):
        assert_model_dim(model, settings)


# --- Property tests (M5 mandate) ------------------------------------------


@given(dim=st.integers(min_value=1, max_value=4096))
def test_assert_model_dim_passes_when_equal(dim: int) -> None:
    """Any dim that matches between model and settings → no raise."""
    settings = _make_settings(hf_embed_dim=dim)
    model = _stub_model(dim)
    assert assert_model_dim(model, settings) is None


@given(
    model_dim=st.integers(min_value=1, max_value=4096),
    env_dim=st.integers(min_value=1, max_value=4096),
)
def test_assert_model_dim_raises_when_unequal(model_dim: int, env_dim: int) -> None:
    """Any pair of distinct dims raises RuntimeError."""
    if model_dim == env_dim:
        return  # trivial — covered by the equality property above
    settings = _make_settings(hf_embed_dim=env_dim)
    model = _stub_model(model_dim)
    with pytest.raises(RuntimeError, match="dim mismatch"):
        assert_model_dim(model, settings)


# --- App boot integration (smoke per process step 8) ---------------------


def test_create_app_boots_when_model_dim_matches() -> None:
    """Lifespan startup runs assert_model_dim; matching dim → app starts."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from biblioplex.main import create_app

    load_model.cache_clear()
    settings = _make_settings(hf_embed_dim=384)
    with patch("biblioplex.embed.model.SentenceTransformer") as ctor:
        ctor.return_value = _stub_model(384)
        app = create_app(settings=settings)
        assert isinstance(app, FastAPI)
        # Drive the lifespan by entering a TestClient context.
        with TestClient(app):
            pass


def test_create_app_fails_when_model_dim_mismatch() -> None:
    """Lifespan startup with mismatched dim → RuntimeError propagates."""
    from fastapi.testclient import TestClient

    from biblioplex.main import create_app

    load_model.cache_clear()
    settings = _make_settings(hf_embed_dim=384)
    with patch("biblioplex.embed.model.SentenceTransformer") as ctor:
        ctor.return_value = _stub_model(7)
        app = create_app(settings=settings)
        with pytest.raises(RuntimeError, match="dim mismatch"), TestClient(app):
            pass
