"""Provide deterministic downstream fixtures without external service calls."""

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
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


app = Starlette(routes=[Route("/mcp", mcp, methods=["POST"])])
