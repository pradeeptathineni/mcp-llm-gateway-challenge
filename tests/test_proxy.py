"""Check authorization, transparent forwarding, and bounded error handling."""

import unittest

import httpx

from gateway.mcp_proxy import create_app


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    """Count downstream calls to verify authorization happens before forwarding."""

    async def asyncSetUp(self):
        """Use independent client and upstream state for each test."""
        self.calls = []
        self.reply = (
            b'{"jsonrpc":"2.0","id":0,"result":{"tools":[],"nextCursor":"next","_meta":{"x":1}}}'
        )
        self.status = 200
        self.error = None

        async def downstream(request):
            """Record exact bytes and headers without accessing a network service."""
            self.calls.append(request)
            if self.error:
                raise self.error
            return httpx.Response(self.status, content=self.reply)

        self.upstream = httpx.AsyncClient(transport=httpx.MockTransport(downstream))
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(client=self.upstream)),
            base_url="http://test",
        )

    async def asyncTearDown(self):
        """Release both clients even after a failing assertion."""
        await self.client.aclose()
        await self.upstream.aclose()

    async def send(self, payload, token="demo-viewer-token"):
        """Use the same authenticated HTTP boundary as real clients."""
        return await self.client.post(
            "/mcp", json=payload, headers={"Authorization": f"Bearer {token}"}
        )

    async def test_list_is_transparent(self):
        """Preserve request metadata, pagination, raw bytes, and response bytes."""
        body = (
            b'{ "jsonrpc":"2.0", "id":0, "method":"tools/list", '
            b'"params":{"cursor":"abc","_meta":{"x":2}} }'
        )
        response = await self.client.post(
            "/mcp", content=body, headers={"Authorization": "Bearer demo-viewer-token"}
        )
        self.assertEqual(response.content, self.reply)
        self.assertEqual(self.calls[0].content, body)
        self.assertNotIn("authorization", self.calls[0].headers)

    async def test_viewer_admin_is_denied(self):
        """Keep the exact denial code and ID and perform zero upstream requests."""
        for request_id in (0, "request-1"):
            response = await self.send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {"name": "admin_reset_key", "role": "admin"},
                }
            )
            self.assertEqual(
                response.json(),
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32001, "message": "Unauthorized Tool Call"},
                },
            )
        self.assertEqual(self.calls, [])

    async def test_admin_and_normal_calls_forward(self):
        """Authorize a configured admin and permit ordinary tools for viewers."""
        for name, token in (
            ("admin_reset_key", "demo-admin-token"),
            ("get_status", "demo-viewer-token"),
        ):
            await self.send(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name}}, token
            )
        self.assertEqual(len(self.calls), 2)

    async def test_invalid_auth(self):
        """Reject forged roles, missing tokens, and duplicate auth headers."""
        for headers in (
            {},
            {"Authorization": "Bearer admin"},
            [
                ("Authorization", "Bearer demo-admin-token"),
                ("Authorization", "Bearer demo-viewer-token"),
            ],
        ):
            response = await self.client.post("/mcp", json={}, headers=headers)
            self.assertEqual(response.status_code, 401)
        self.assertEqual(self.calls, [])

    async def test_notifications_and_batches(self):
        """Prevent alternate envelopes from bypassing the privilege check."""
        response = await self.send(
            {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "admin_reset_key"}}
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.content, b"")
        response = await self.send(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "admin_reset_key"},
                }
            ]
        )
        self.assertEqual(response.json()["error"]["code"], -32600)
        self.assertEqual(self.calls, [])
        response = await self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.content, b"")
        self.assertEqual(len(self.calls), 1)

    async def test_malformed_and_large(self):
        """Reject ambiguous objects, invalid shapes, and oversized requests."""
        for body in (b"{", b'{"a":1,"a":2}', b'{"a":NaN}', b"x" * 65537):
            response = await self.client.post(
                "/mcp", content=body, headers={"Authorization": "Bearer demo-viewer-token"}
            )
            self.assertEqual(response.status_code, 400)
        for payload in (
            {"jsonrpc": "1.0", "method": "tools/list", "id": 1},
            {"jsonrpc": "2.0", "method": "tools/list", "id": True},
        ):
            response = await self.send(payload)
            self.assertEqual(response.json()["error"]["code"], -32600)
        self.assertEqual(self.calls, [])

    async def test_sanitized_upstream_failures(self):
        """Never expose an upstream error body, exception, or stack trace."""
        self.status = 500
        self.reply = b"private provider stack trace"
        request = {"jsonrpc": "2.0", "id": 7, "method": "tools/list"}
        response = await self.send(request)
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("private", response.text)
        self.error = httpx.ReadTimeout("private secret")
        response = await self.send(request)
        self.assertEqual(response.status_code, 504)
        self.assertNotIn("private", response.text)
