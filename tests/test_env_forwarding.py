"""D16.9 — pin every ``Settings`` field is forwarded to the api container.

`.env` alone does not reach the api process. Docker-compose interpolates
``${...}`` inside the compose file itself but only forwards keys named
under ``services.api.environment:``. When a new knob is added to
:class:`biblioplex.config.Settings` without a matching entry in
``docker-compose.yml``, the container silently reads the Pydantic default
— ``/health`` shows nothing wrong and the operator's ``.env`` override
is ignored.

This has bitten ``COMBOS_SOURCE_URL``, ``SEARCH_DEFAULT_FILTER``, and
``DEFAULT_Q`` during past dev iterations; the debug ritual was
``docker compose exec api env | grep <VAR>``. This module parses
``docker-compose.yml`` as text (no PyYAML dep — mirrors D10.10's
``test_schema_indexes.py`` no-live-Postgres pattern) and asserts every
Settings field name (uppercased) is present in the api service's
``environment:`` block.

Subset relation, not equality: the compose block may carry extras that
have no Settings counterpart (e.g. ``HF_HOME`` for the HuggingFace cache
volume mount — set by ops, not the pydantic model).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from biblioplex.config import Settings

COMPOSE_PATH = Path(__file__).resolve().parent.parent / "docker-compose.yml"


@pytest.fixture(scope="module")
def api_env_keys() -> set[str]:
    """Extract keys from the ``api.environment:`` block of docker-compose.yml.

    Slice the ``api:`` service block out of the top-level ``services:`` map,
    then slice the ``environment:`` block out of that, then read every
    ``UPPER_KEY:`` line inside. No YAML parser — the file's shape is
    stable and the regex fails loudly if it drifts.
    """
    text = COMPOSE_PATH.read_text()
    api_match = re.search(
        r"^\s{2}api:\n(.*?)(?=^\s{2}\w+:\n|^volumes:)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert api_match is not None, "api service block not found in docker-compose.yml"
    api_block = api_match.group(1)
    env_match = re.search(
        r"^\s{4}environment:\n(.*?)(?=^\s{4}\w+:\n|\Z)",
        api_block,
        re.MULTILINE | re.DOTALL,
    )
    assert env_match is not None, "api.environment block not found"
    env_block = env_match.group(1)
    return {
        m.group(1)
        for m in re.finditer(
            r"^\s{6}([A-Z_][A-Z0-9_]*):", env_block, re.MULTILINE
        )
    }


def test_all_settings_fields_forwarded_to_api_container(
    api_env_keys: set[str],
) -> None:
    """Every ``Settings`` field must appear in ``api.environment``.

    The uppercased field name is the pydantic-settings convention (env var
    lookup is case-insensitive via ``case_sensitive=False`` on the config).
    Missing entries mean the container silently uses the default.
    """
    field_upper = {name.upper() for name in Settings.model_fields}
    missing = field_upper - api_env_keys
    assert not missing, (
        f"Settings fields not forwarded to api container: {sorted(missing)}. "
        "Add each as `<NAME>: ${<NAME>:-<default>}` under services.api.environment "
        "in docker-compose.yml. `.env` alone does not reach the api process."
    )


def test_api_env_keys_extractor_sanity(api_env_keys: set[str]) -> None:
    """Regression guard on the extractor itself.

    Prevents a lax regex silently returning ``set()`` — which would make
    the forwarding assertion tautologically pass regardless of drift.
    Every current Settings field IS forwarded, so ``api_env_keys`` must
    at minimum cover the fields plus one ops-only extra (``HF_HOME``).
    """
    assert len(api_env_keys) >= 10, (
        f"api.environment extractor returned only {len(api_env_keys)} keys "
        f"({sorted(api_env_keys)}) — regex likely broken."
    )
