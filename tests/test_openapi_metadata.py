"""D8.2 OpenAPI polish — schema exposes the documented tags + operations.

The test asserts the surface an agent walks when bootstrapping against
``/openapi.json``: the three tag groups (``cards`` / ``admin`` / ``health``),
the operationIds for every endpoint, and the presence of a documented
``ErrorEnvelope`` component so 4xx response shapes show up in the schema
instead of bare ``{}``.

We deliberately do NOT snapshot the full schema — it would churn every
time FastAPI or the project tweaks an unrelated default. The asserts
target only the fields the README + agent-facing notes commit to.

The schema is fetched via ``app.openapi()`` directly (a pure function of
the route table). Going through ``TestClient`` would drive the lifespan
and trigger a real SentenceTransformer load, which is irrelevant to what
this test is checking.
"""

from __future__ import annotations

from typing import Any

from biblioplex import __version__
from biblioplex.config import Settings
from biblioplex.main import create_app


def _schema() -> dict[str, Any]:
    """Build the app and return its computed OpenAPI schema."""
    settings = Settings.model_construct(
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
    app = create_app(settings=settings)
    schema: dict[str, Any] = app.openapi()
    return schema


def test_openapi_info_block_present() -> None:
    """`info` carries title/version/description per D8.2."""
    schema = _schema()
    info = schema["info"]
    assert info["title"] == "Biblioplex"
    assert info["version"] == __version__
    assert info["description"], "description must not be empty"


def test_openapi_schema_top_level_keys() -> None:
    """Smoke: the schema has `openapi`, `paths`, and `components` populated."""
    schema = _schema()
    assert schema.get("openapi", "").startswith("3."), schema.get("openapi")
    assert schema.get("paths"), "paths must not be empty"
    assert schema.get("components", {}).get("schemas"), "no component schemas"


def test_openapi_tag_groups_documented() -> None:
    """`tags` lists the three documented groups with descriptions."""
    schema = _schema()
    tags = {t["name"]: t for t in schema.get("tags", [])}
    for name in ("cards", "admin", "health"):
        assert name in tags, f"missing tag group: {name}"
        assert tags[name].get("description"), f"tag '{name}' missing description"


def _operation_ids(schema: dict[str, Any]) -> set[str]:
    """Flatten every operationId in the schema for membership asserts."""
    out: set[str] = set()
    for methods in schema["paths"].values():
        for op in methods.values():
            if isinstance(op, dict) and "operationId" in op:
                out.add(op["operationId"])
    return out


def test_every_endpoint_has_an_operation_id() -> None:
    """Each documented endpoint exposes a stable operationId.

    D17.1 collapses FastAPI's auto-generated `<func>_<path>_<method>`
    ids down to just the route function name via
    `generate_unique_id_function` — so MCP tool names read
    `search_cards`, not `search_cards_cards_search_get`.
    """
    schema = _schema()
    op_ids = _operation_ids(schema)
    expected = {
        "health",
        "trigger_refresh",
        "search_cards",
        "get_card_by_name",
        "get_similar_cards",
        "get_card_by_id",
    }
    missing = expected - op_ids
    assert not missing, f"missing operationIds: {missing}"


def test_error_envelope_component_present() -> None:
    """D8.2 ships an `ErrorEnvelope` schema so 4xx bodies are documented."""
    schema = _schema()
    components = schema.get("components", {}).get("schemas", {})
    assert "ErrorEnvelope" in components, (
        "ErrorEnvelope schema must be registered so 4xx response bodies "
        "are documented in the OpenAPI surface"
    )
    props = components["ErrorEnvelope"]["properties"]
    for field in ("object", "status", "code", "details"):
        assert field in props, f"ErrorEnvelope missing field: {field}"


def test_search_endpoint_documents_404_and_422() -> None:
    """`/cards/search` advertises its documented error responses."""
    schema = _schema()
    responses = schema["paths"]["/cards/search"]["get"]["responses"]
    # 200 always present (FastAPI default); we care about the error codes.
    for code in ("404", "422"):
        assert code in responses, f"/cards/search missing {code} response"
