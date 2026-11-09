"""Provide deterministic downstream fixtures without external service calls."""

import asyncio
import json

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route


async def mcp(request: Request):
    """Expose a normal tool and a privileged tool for proxy verification."""
    payload = await request.json()
    if "id" not in payload:
        return Response(status_code=202)
    if payload["method"] == "tools/list":
        result = {
            "tools": [
                {"name": "get_status", "inputSchema": {"type": "object"}},
                {"name": "admin_reset_key", "inputSchema": {"type": "object"}},
            ],
            "nextCursor": "demo-page-2",
            "_meta": {"source": "mock"},
        }
    else:
        result = {"content": [{"type": "text", "text": "Synthetic tool executed"}]}
    return JSONResponse({"jsonrpc": "2.0", "id": payload["id"], "result": result})


async def stream(request: Request):
    """Split synthetic PII across deltas to expose chunk-boundary mistakes."""

    async def chunks():
        """Delay chunks so tests can measure output before stream completion."""
        text = (
            "Hello café. Contact person@example.com, SSN 123-45-6789, "
            "card 4111 1111 1111 1111. Done."
        )
        for offset in range(0, len(text), 3):
            payload = {"choices": [{"delta": {"content": text[offset : offset + 3]}}]}
            yield ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode()
            await asyncio.sleep(0.01)
        yield b"data: [DONE]\n\n"

    return StreamingResponse(chunks(), media_type="text/event-stream")


app = Starlette(
    routes=[Route("/mcp", mcp, methods=["POST"]), Route("/stream", stream, methods=["POST"])]
)
