"""Shared Scryfall-shaped error envelope (§6, D8.2).

Every 4xx response from this service uses the same body shape that
``api.scryfall.com`` returns. Centralising the Pydantic model lets the
routers reference it from ``responses={…}`` so OpenAPI documents 4xx
bodies instead of leaving them as bare ``{}``.

We don't *return* ``ErrorEnvelope`` instances at runtime — the routers
keep emitting raw ``JSONResponse``s for performance and to avoid a
Pydantic round-trip on the error path. The model exists purely as the
schema FastAPI references when it walks ``responses={…}`` for the
OpenAPI document.
"""

from __future__ import annotations

from typing import Literal

from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


def scryfall_error(status_code: int, code: str, details: str) -> JSONResponse:
    """Emit the Scryfall-shaped 4xx envelope.

    Routers keep returning ``JSONResponse`` directly (not
    ``ErrorEnvelope`` instances) to skip the Pydantic round-trip on the
    hot error path — the model above only documents the schema.
    """
    return JSONResponse(
        status_code=status_code,
        content={
            "object": "error",
            "status": status_code,
            "code": code,
            "details": details,
        },
    )


class ErrorEnvelope(BaseModel):
    """Scryfall-compatible error body — see §6 of ``tech_spec.md``.

    Example::

        {"object": "error", "status": 404, "code": "not_found",
         "details": "No card found with id '…'"}
    """

    object: Literal["error"] = Field(
        default="error",
        description="Always the literal ``error`` so clients can branch on it.",
    )
    status: int = Field(description="HTTP status code, mirrored in the body.")
    code: str = Field(
        description=(
            "Stable slug describing the failure. Known values: "
            "``not_found``, ``bad_request``, ``unsupported_format``, "
            "``unsupported_operator``, ``invalid_projection``."
        )
    )
    details: str = Field(description="Human-readable diagnostic.")


__all__ = ["ErrorEnvelope", "scryfall_error"]
