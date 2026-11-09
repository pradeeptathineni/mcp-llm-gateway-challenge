"""Serve an authenticated streaming gateway against an explicit provider URL."""

import os
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from gateway.http import DEMO_TOKENS, Identity, InvalidPayload, authenticate, parse_json, read_body
from gateway.streaming import guarded_stream


class StreamRequest(BaseModel):
    """Bound prompts and reject unrecognized provider or routing fields."""

    model_config = ConfigDict(strict=True, extra="forbid")
    prompt: Annotated[str, Field(min_length=1, max_length=4096)]


def gateway_error(code: str, message: str, status: int) -> JSONResponse:
    """Expose stable application errors without provider diagnostics."""
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def create_app(
    provider_url: str = "http://127.0.0.1:8011/stream",
    tokens: dict[str, Identity] | None = None,
    client: httpx.AsyncClient | None = None,
) -> Starlette:
    """Keep provider configuration outside request-controlled input."""
    identities = DEMO_TOKENS if tokens is None else tokens

    @asynccontextmanager
    async def lifespan(app):
        """Reuse one bounded-connection client and close owned resources."""
        async with httpx.AsyncClient(timeout=5, trust_env=False, follow_redirects=False) as owned:
            app.state.client = client or owned
            yield

    async def stream(request: Request):
        """Validate before opening upstream and stream only normalized safe deltas."""
        if authenticate(request, identities) is None:
            return gateway_error("unauthorized", "Authentication required", 401)
        try:
            payload = StreamRequest.model_validate(parse_json(await read_body(request)))
        except (InvalidPayload, ValidationError):
            return gateway_error("invalid_request", "Invalid stream request", 400)
        transport = client or request.app.state.client
        try:
            response = await transport.send(
                transport.build_request(
                    "POST", provider_url, json={"prompt": payload.prompt, "stream": True}
                ),
                stream=True,
            )
        except httpx.HTTPError:
            return gateway_error("provider_unavailable", "Provider unavailable", 502)
        if response.status_code != 200 or not response.headers.get("content-type", "").startswith(
            "text/event-stream"
        ):
            await response.aclose()
            return gateway_error("provider_unavailable", "Provider unavailable", 502)
        return StreamingResponse(
            guarded_stream(response),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    return Starlette(routes=[Route("/v1/stream", stream, methods=["POST"])], lifespan=lifespan)


app = create_app(os.environ.get("LLM_STREAM_URL", "http://127.0.0.1:8011/stream"))
