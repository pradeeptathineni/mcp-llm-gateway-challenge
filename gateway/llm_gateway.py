"""Serve an authenticated streaming gateway against an explicit provider URL."""

import asyncio
import os
import sqlite3
from contextlib import asynccontextmanager

import httpx
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from gateway.http import DEMO_TOKENS, Identity, InvalidPayload, authenticate, parse_json, read_body
from gateway.limiter import QuotaExceeded, TokenLimiter
from gateway.router import CompletionRequest, ProviderFailure, Router
from gateway.streaming import guarded_stream


def gateway_error(code: str, message: str, status: int) -> JSONResponse:
    """Expose stable application errors without provider diagnostics."""
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def create_app(
    provider_url: str = "http://127.0.0.1:8011/stream",
    tokens: dict[str, Identity] | None = None,
    client: httpx.AsyncClient | None = None,
    limiter: TokenLimiter | None = None,
    database: str = "var/gateway.sqlite3",
    primary: str = "http://127.0.0.1:8011/primary",
    backup: str = "http://127.0.0.1:8011/backup",
) -> Starlette:
    """Keep provider configuration outside request-controlled input."""
    identities = DEMO_TOKENS if tokens is None else tokens

    @asynccontextmanager
    async def lifespan(app):
        """Reuse one bounded-connection client and close owned resources."""
        async with httpx.AsyncClient(timeout=5, trust_env=False, follow_redirects=False) as owned:
            app.state.client = client or owned
            app.state.limiter = limiter or await asyncio.to_thread(TokenLimiter, database)
            yield

    async def stream(request: Request):
        """Validate before opening upstream and stream only normalized safe deltas."""
        identity = authenticate(request, identities)
        if identity is None:
            return gateway_error("unauthorized", "Authentication required", 401)
        try:
            payload = CompletionRequest.model_validate(parse_json(await read_body(request)))
        except (InvalidPayload, ValidationError):
            return gateway_error("invalid_request", "Invalid stream request", 400)
        transport = client or request.app.state.client
        budget = limiter or request.app.state.limiter
        try:
            await budget.reserve(identity.tenant, payload.reservation_tokens())
        except QuotaExceeded as failure:
            result = gateway_error("rate_limited", "Token budget exceeded", 429)
            result.headers["Retry-After"] = str(failure.retry_after)
            return result
        except (sqlite3.Error, ValueError):
            return gateway_error("quota_unavailable", "Token accounting unavailable", 503)
        try:
            response = await transport.send(
                transport.build_request(
                    "POST", provider_url, json={**payload.model_dump(), "stream": True}
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
            guarded_stream(response, max_output_bytes=payload.max_tokens),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    async def complete(request: Request):
        """Apply tenant admission, total-deadline fallback, and safe error translation."""
        identity = authenticate(request, identities)
        if identity is None:
            return gateway_error("unauthorized", "Authentication required", 401)
        try:
            payload = CompletionRequest.model_validate(parse_json(await read_body(request)))
        except (InvalidPayload, ValidationError):
            return gateway_error("invalid_request", "Invalid completion request", 400)
        router = Router(
            client or request.app.state.client,
            limiter or request.app.state.limiter,
            primary,
            backup,
        )
        try:
            return JSONResponse(await router.complete(identity.tenant, payload))
        except QuotaExceeded as failure:
            result = gateway_error("rate_limited", "Token budget exceeded", 429)
            result.headers["Retry-After"] = str(failure.retry_after)
            return result
        except (sqlite3.Error, ValueError):
            return gateway_error("quota_unavailable", "Token accounting unavailable", 503)
        except ProviderFailure:
            return gateway_error("provider_unavailable", "Provider unavailable", 502)

    return Starlette(
        routes=[
            Route("/v1/stream", stream, methods=["POST"]),
            Route("/v1/completions", complete, methods=["POST"]),
        ],
        lifespan=lifespan,
    )


app = create_app(
    os.environ.get("LLM_STREAM_URL", "http://127.0.0.1:8011/stream"),
    database=os.environ.get("GATEWAY_DATABASE", "var/gateway.sqlite3"),
    primary=os.environ.get("LLM_PRIMARY_URL", "http://127.0.0.1:8011/primary"),
    backup=os.environ.get("LLM_BACKUP_URL", "http://127.0.0.1:8011/backup"),
)
