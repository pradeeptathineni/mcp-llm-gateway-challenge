"""Shared bounded JSON and authentication helpers."""

import hmac
import json
from dataclasses import dataclass

from starlette.requests import Request
from starlette.responses import JSONResponse, Response


@dataclass(frozen=True)
class Identity:
    """Keep authorization and tenant scope in server-controlled configuration."""

    tenant: str
    role: str


# These public fixtures are exclusively for localhost demonstrations
DEMO_TOKENS = {
    "demo-viewer-token": Identity("demo-viewer", "viewer"),
    "demo-admin-token": Identity("demo-admin", "admin"),
}


class InvalidPayload(ValueError):
    """Report a safe parse failure without retaining the submitted body."""


async def read_body(request: Request, limit: int = 65_536) -> bytes:
    """Apply the limit while consuming, including chunked request bodies."""
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise InvalidPayload("Request too large")
        body.extend(chunk)
    return bytes(body)


def parse_json(body: bytes):
    """Reject ambiguous duplicate keys and nonstandard numeric constants."""

    def pairs(items):
        """Keep one meaning for each object member."""
        result = {}
        for key, value in items:
            if key in result:
                raise InvalidPayload("Duplicate JSON key")
            result[key] = value
        return result

    def constant(_value):
        """Keep NaN and Infinity outside the JSON wire format."""
        raise InvalidPayload("Invalid JSON number")

    try:
        return json.loads(body, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise InvalidPayload("Invalid JSON") from None


def authenticate(request: Request, tokens: dict[str, Identity]) -> Identity | None:
    """Resolve an opaque bearer token without trusting caller-supplied roles."""
    headers = request.headers.getlist("authorization")
    if len(headers) != 1:
        return None
    parts = headers[0].split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    for token, identity in tokens.items():
        if hmac.compare_digest(parts[1].encode(), token.encode()):
            return identity
    return None


def rpc_error(request_id, code: int, message: str, status: int = 200) -> JSONResponse:
    """Keep gateway errors stable and independent of upstream exception text."""
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}},
        status_code=status,
    )


def denied_notification(status: int) -> Response:
    """Notifications have HTTP status but no JSON-RPC response body."""
    return Response(status_code=status)
