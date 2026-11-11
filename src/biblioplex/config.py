"""Process-wide settings loaded from environment per tech_spec.md §10."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-backed configuration.

    All knobs from §10 of the tech spec. ``DATABASE_URL`` and
    ``HF_EMBED_DIM`` are required; the rest carry production-safe defaults
    matching the docker-compose declarations.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    database_url: str
    hf_embed_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    hf_embed_dim: int = 384
    hf_embed_device: str = "cpu"
    embed_batch: int = 256
    bulk_source_type: str = "default_cards"
    web_concurrency: int = 1
    cors_origins: str = ""
    log_level: str = "INFO"
    # Optional DSL fragment AND-ed onto every /cards/search query before parse.
    # Use to scope a deployment to a specific format (e.g. "f:commander").
    search_default_filter: str = ""
    # When true, lifespan kicks /admin/refresh in the background if the
    # cards table is empty at boot. Set false on read-only replicas.
    auto_refresh_if_empty: bool = True
    # D14.1 — Commander Spellbook bulk file (variants.json.gz), on by
    # default. Set to empty to disable the combo ingest step in the
    # refresh runner.
    combos_source_url: str = "https://json.commanderspellbook.com/variants.json.gz"
    # D16.16 — Bearer token gating the /mcp mount. Empty (default) leaves
    # the transport open for local dev; set to a strong random hex in prod
    # so anon callers can't grab the read tool catalog + corpus. Generate
    # with `openssl rand -hex 32`. Compose forwarding rule per CLAUDE.md
    # Env vars section — add to services.api.environment: too.
    mcp_auth_token: str = ""


def get_settings() -> Settings:
    """Build a fresh :class:`Settings` from the current environment.

    Not memoised — tests override env via monkeypatch and want each call to
    re-read. Production code calls this once at startup.
    """
    return Settings()  # type: ignore[call-arg]
