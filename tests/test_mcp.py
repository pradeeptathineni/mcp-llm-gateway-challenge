"""Exercise the actual child process and every declared input boundary."""

import asyncio
import json
import sys
import unittest
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError
from pydantic import ValidationError

from gateway.mcp_server import CustomerInput, RefundInput, safe_call_tool


class SchemaTests(unittest.TestCase):
    """Reject malformed values without coercing caller intent."""

    def test_customer_validation(self):
        """Check type, syntax, length, and undeclared fields."""
        for value in (
            None,
            123,
            True,
            "CUST-1234",
            "cust-12345",
            "CUST-12345\n",
            "CUST-１２３４５",
        ):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                CustomerInput(customer_id=value)
        with self.assertRaises(ValidationError):
            CustomerInput(customer_id="CUST-00001", extra="ignored?")

    def test_refund_validation(self):
        """Disallow nonfinite, nonpositive, boolean, and coerced amounts."""
        for amount in (0, -1, True, "1.5", float("nan"), float("inf")):
            with self.subTest(amount=amount), self.assertRaises(ValidationError):
                RefundInput(customer_id="CUST-00001", amount=amount, reason="Valid reason here")
        for reason in ("short", "          ", None, 123):
            with self.subTest(reason=reason), self.assertRaises(ValidationError):
                RefundInput(customer_id="CUST-00001", amount=1.5, reason=reason)
        self.assertEqual(
            RefundInput(customer_id="CUST-00001", amount=2, reason="Valid reason here").amount, 2.0
        )


class StdioTests(unittest.IsolatedAsyncioTestCase):
    """Verify a real stdio session rather than invoking handlers in isolation."""

    async def test_official_client(self):
        """Negotiate the pinned SDK protocol and execute both synthetic tools."""
        params = StdioServerParameters(command=sys.executable, args=["-m", "gateway.mcp_server"])
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                initialized = await session.initialize()
                self.assertTrue(initialized.protocolVersion)
                tools = await session.list_tools()
                self.assertEqual(
                    {t.name for t in tools.tools}, {"get_customer_record", "trigger_refund"}
                )
                record = await session.call_tool(
                    "get_customer_record", {"customer_id": "CUST-00001"}
                )
                self.assertFalse(record.isError)
                refund = await session.call_tool(
                    "trigger_refund",
                    {
                        "customer_id": "CUST-00001",
                        "amount": 1.25,
                        "reason": "Duplicate test charge",
                    },
                )
                self.assertEqual(refund.structuredContent["status"], "simulated")
                missing = await session.call_tool(
                    "get_customer_record", {"customer_id": "CUST-99999"}
                )
                self.assertTrue(missing.isError)

    async def test_wire_errors_and_stdout(self):
        """Assert exact JSON-RPC codes, IDs, and protocol-only stdout."""
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "gateway.mcp_server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:

            async def exchange(payload):
                """Bound each wire read so a transport regression cannot hang the suite."""
                process.stdin.write((json.dumps(payload) + "\n").encode())
                await process.stdin.drain()
                return json.loads(await asyncio.wait_for(process.stdout.readline(), 5))

            reply = await exchange(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1.0"},
                    },
                }
            )
            self.assertIn("result", reply)
            process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            for request_id, name, arguments in (
                (2, "get_customer_record", {"customer_id": "bad"}),
                (
                    3,
                    "trigger_refund",
                    {"customer_id": "CUST-00001", "amount": "1", "reason": "Valid reason here"},
                ),
                (4, "get_customer_record", {"customer_id": "CUST-00001", "extra": 1}),
                (5, "missing_tool", {}),
            ):
                reply = await exchange(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": arguments},
                    }
                )
                self.assertEqual(reply["id"], request_id)
                self.assertEqual(reply["error"]["code"], -32602)
            reply = await exchange({"jsonrpc": "2.0", "id": 6, "method": "unknown/method"})
            self.assertEqual(reply["error"]["code"], -32602)
            process.stdin.close()
            await asyncio.wait_for(process.wait(), 5)
            self.assertEqual(process.returncode, 0)
            self.assertEqual(await process.stdout.read(), b"")
            self.assertIn(b"Starting synthetic", await process.stderr.read())
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def test_unexpected_tool_error_is_sanitized(self):
        """Do not expose exception details through the SDK's fallback error path."""
        request = types.CallToolRequest(
            method="tools/call",
            params=types.CallToolRequestParams(
                name="trigger_refund",
                arguments={
                    "customer_id": "CUST-00001",
                    "amount": 1.0,
                    "reason": "Valid reason here",
                },
            ),
        )
        with patch("gateway.mcp_server.uuid4", side_effect=RuntimeError("private credential")):
            with self.assertRaises(McpError) as caught:
                await safe_call_tool(request)
        self.assertEqual(caught.exception.error.code, -32603)
        self.assertNotIn("private", caught.exception.error.message)
