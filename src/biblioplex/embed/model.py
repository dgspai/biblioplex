"""SentenceTransformer loader per tech_spec.md §8.1.

Lazy, process-wide singleton. The first call (cache miss only) downloads the HuggingFace
weights to ``~/.cache/huggingface`` (per §8.1 — no Docker build-time
pre-cache); subsequent calls return the same in-memory model.

The cache key is the ``(model_name, device)`` tuple derived from
:class:`~biblioplex.config.Settings`. In practice a process sees one
settings object, so the singleton holds for the process lifetime. Tests
call :meth:`functools.lru_cache.cache_clear` between cases to isolate
the constructor-call assertions.
"""

from __future__ import annotations

from functools import lru_cache

from sentence_transformers import SentenceTransformer

from biblioplex.config import Settings


@lru_cache(maxsize=1)
def _load_model_cached(model_name: str, device: str) -> SentenceTransformer:
    """Construct the SentenceTransformer once per (model_name, device).

    Tries the local HF cache first so a boot with a cached model makes no
    request to huggingface.co; only a cache miss falls back to a download.
    """
    model: SentenceTransformer
    try:
        model = SentenceTransformer(model_name, device=device, local_files_only=True)
    except OSError:  # not cached yet (first start)
        model = SentenceTransformer(model_name, device=device)
    return model


def load_model(settings: Settings) -> SentenceTransformer:
    """Return the process-wide SentenceTransformer instance.

    Args:
        settings: Resolved :class:`Settings` — supplies ``hf_embed_model``
            and ``hf_embed_device``.

    Returns:
        The cached :class:`SentenceTransformer`. Subsequent calls with the
        same settings return the same instance — no re-download.
    """
    return _load_model_cached(settings.hf_embed_model, settings.hf_embed_device)


# Expose the underlying lru_cache for tests that need to reset state.
load_model.cache_clear = _load_model_cached.cache_clear  # type: ignore[attr-defined]


def assert_model_dim(model: SentenceTransformer, settings: Settings) -> None:
    """Fail-fast if the loaded model's dimension disagrees with ``HF_EMBED_DIM``.

    Per §8.1, ``HF_EMBED_DIM`` is required configuration; it must match
    ``model.get_sentence_embedding_dimension()`` because the ``cards.embedding``
    column is declared ``vector(D)`` with ``D`` baked into ``schema.sql``.
    A mismatch would silently corrupt every embed write, so we raise at
    boot — before any request can hit the DB — to surface the misconfig.

    Args:
        model: The loaded :class:`SentenceTransformer`.
        settings: Resolved :class:`Settings` (source of ``HF_EMBED_DIM``).

    Raises:
        RuntimeError: When ``model.get_sentence_embedding_dimension()``
            does not equal ``settings.hf_embed_dim``.
    """
    actual = model.get_sentence_embedding_dimension()
    expected = settings.hf_embed_dim
    if actual != expected:
        raise RuntimeError(
            f"dim mismatch: model={actual}, env={expected}"
        )
