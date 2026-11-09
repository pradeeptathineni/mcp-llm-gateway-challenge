"""Exercise HTTP behavior through separate local server processes."""

import json
import time
import unittest

import httpx
from support import running_server


class HttpTests(unittest.IsolatedAsyncioTestCase):
    """Include real sockets, lifespan handling, and HTTPX forwarding."""

    async def test_mcp_proxy(self):
        """Check transparent discovery and both sides of the admin boundary."""
        async with running_server("gateway.mock_server:app") as downstream:
            async with running_server(
                "gateway.mcp_proxy:app", {"MCP_DOWNSTREAM_URL": downstream + "/mcp"}
            ) as proxy:
                async with httpx.AsyncClient(trust_env=False) as client:
                    headers = {"Authorization": "Bearer demo-viewer-token"}
                    payload = {"jsonrpc": "2.0", "id": "tcp", "method": "tools/list"}
                    direct = await client.post(downstream + "/mcp", json=payload)
                    result = await client.post(proxy + "/mcp", json=payload, headers=headers)
                    self.assertEqual(result.content, direct.content)
                    payload.update(method="tools/call", params={"name": "admin_reset_key"})
                    result = await client.post(proxy + "/mcp", json=payload, headers=headers)
                    self.assertEqual(result.json()["error"]["code"], -32001)
                    headers["Authorization"] = "Bearer demo-admin-token"
                    result = await client.post(proxy + "/mcp", json=payload, headers=headers)
                    self.assertIn("result", result.json())

    async def test_stream_redaction(self):
        """Observe safe output before the upstream completes over real sockets."""
        async with running_server("gateway.mock_server:app") as downstream:
            async with running_server(
                "gateway.llm_gateway:app", {"LLM_STREAM_URL": downstream + "/stream"}
            ) as proxy:
                async with httpx.AsyncClient(trust_env=False) as client:
                    start = time.monotonic()
                    first = None
                    output = []
                    done = False
                    async with client.stream(
                        "POST",
                        proxy + "/v1/stream",
                        json={"prompt": "demo"},
                        headers={"Authorization": "Bearer demo-viewer-token"},
                    ) as response:
                        self.assertEqual(response.status_code, 200)
                        async for line in response.aiter_lines():
                            if line.startswith("data: "):
                                payload = json.loads(line[6:])
                                if "delta" in payload:
                                    first = first or time.monotonic()
                                    output.append(payload["delta"])
                                done |= payload.get("done", False)
                    elapsed = time.monotonic() - start
                    self.assertTrue(done)
                    self.assertEqual(
                        "".join(output),
                        "Hello café. Contact [REDACTED], SSN [REDACTED], card [REDACTED]. Done.",
                    )
                    self.assertLess(first - start, elapsed * 0.8)
                    capped = await client.post(
                        proxy + "/v1/stream",
                        json={"prompt": "demo", "max_tokens": 1},
                        headers={"Authorization": "Bearer demo-viewer-token"},
                    )
                    self.assertIn("upstream_stream_error", capped.text)
                    self.assertNotIn('"done": true', capped.text)

    async def test_completion_fallback(self):
        """Verify the default three-second deadline through independent HTTP services."""
        async with running_server("gateway.mock_server:app") as downstream:
            environment = {
                "LLM_PRIMARY_URL": downstream + "/primary",
                "LLM_BACKUP_URL": downstream + "/backup",
            }
            async with running_server("gateway.llm_gateway:app", environment) as proxy:
                async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
                    headers = {"Authorization": "Bearer demo-viewer-token"}
                    for prompt, expected in (
                        ("demo", "primary"),
                        ("primary-rate-limit", "backup"),
                        ("primary-timeout", "backup"),
                    ):
                        start = time.monotonic()
                        response = await client.post(
                            proxy + "/v1/completions", json={"prompt": prompt}, headers=headers
                        )
                        elapsed = time.monotonic() - start
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.json()["model"], expected)
                        if prompt == "primary-timeout":
                            self.assertGreaterEqual(elapsed, 2.9)
                            self.assertLess(elapsed, 5)
