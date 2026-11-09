"""Authorize tool calls before forwarding unchanged JSON-RPC requests."""

import os
from contextlib import asynccontextmanager

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from gateway.http import (
    DEMO_TOKENS,
    Identity,
    InvalidPayload,
    authenticate,
    denied_notification,
    parse_json,
    read_body,
    rpc_error,
)


def create_app(
    downstream_url: str = "http://127.0.0.1:8011/mcp",
    tokens: dict[str, Identity] | None = None,
    client: httpx.AsyncClient | None = None,
) -> Starlette:
    """Create an isolated proxy with an explicit upstream and trusted identities."""
    identities = DEMO_TOKENS if tokens is None else tokens

    @asynccontextmanager
    async def lifespan(app):
        """Close only the HTTP client owned by this application."""
        async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as owned:
            app.state.client = client or owned
            yield

    async def proxy(request: Request) -> Response:
        """Reject malformed and forbidden requests before any upstream I/O."""
        identity = authenticate(request, identities)
        if identity is None:
            response = rpc_error(None, -32000, "Authentication required", 401)
            response.headers["WWW-Authenticate"] = "Bearer"
            return response
        try:
            body = await read_body(request)
            payload = parse_json(body)
        except InvalidPayload:
            return rpc_error(None, -32700, "Invalid JSON payload", 400)
        if not isinstance(payload, dict):
            return rpc_error(None, -32600, "Expected one JSON-RPC request", 400)
        request_id = payload.get("id")
        if (
            payload.get("jsonrpc") != "2.0"
            or not isinstance(payload.get("method"), str)
            or (
                "id" in payload
                and (isinstance(request_id, bool) or not isinstance(request_id, (str, int)))
            )
        ):
            return rpc_error(None, -32600, "Invalid JSON-RPC request", 400)
        notification = "id" not in payload
        params = payload.get("params", {})
        if not isinstance(params, dict):
            return (
                denied_notification(400)
                if notification
                else rpc_error(request_id, -32602, "Invalid parameters")
            )
        if payload["method"] == "tools/call":
            name = params.get("name")
            if not isinstance(name, str):
                return (
                    denied_notification(400)
                    if notification
                    else rpc_error(request_id, -32602, "Invalid tool name")
                )
            if name.startswith("admin_") and identity.role != "admin":
                return (
                    denied_notification(403)
                    if notification
                    else rpc_error(request_id, -32001, "Unauthorized Tool Call")
                )
        try:
            # Do not forward caller credentials or accept per-request upstream URLs
            transport = client or request.app.state.client
            async with transport.stream(
                "POST", downstream_url, content=body, headers={"Content-Type": "application/json"}
            ) as upstream:
                if upstream.status_code >= 400:
                    return (
                        denied_notification(502)
                        if notification
                        else rpc_error(request_id, -32603, "Downstream unavailable", 502)
                    )
                result = bytearray()
                async for chunk in upstream.aiter_bytes():
                    if len(result) + len(chunk) > 1_048_576:
                        return (
                            denied_notification(502)
                            if notification
                            else rpc_error(request_id, -32603, "Downstream response too large", 502)
                        )
                    result.extend(chunk)
                if notification:
                    return Response(status_code=202)
                return Response(
                    bytes(result), status_code=upstream.status_code, media_type="application/json"
                )
        except httpx.TimeoutException:
            return (
                denied_notification(504)
                if notification
                else rpc_error(request_id, -32002, "Downstream timeout", 504)
            )
        except httpx.HTTPError:
            return (
                denied_notification(502)
                if notification
                else rpc_error(request_id, -32603, "Downstream unavailable", 502)
            )

    return Starlette(routes=[Route("/mcp", proxy, methods=["POST"])], lifespan=lifespan)


# Default deployment is a localhost demo, with explicitly documented fixture tokens
app = create_app(os.environ.get("MCP_DOWNSTREAM_URL", "http://127.0.0.1:8011/mcp"))
