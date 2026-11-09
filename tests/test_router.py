"""Verify durable admission, concurrency, total deadlines, and failure cleanup."""

import asyncio
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import httpx

from gateway.limiter import QuotaExceeded, TokenLimiter
from gateway.llm_gateway import create_app
from gateway.router import CompletionRequest, ProviderFailure, Router


class LimiterTests(unittest.IsolatedAsyncioTestCase):
    """Use real temporary SQLite files and independent database connections."""

    async def asyncSetUp(self):
        """Inject epoch time to test exact window boundaries without sleeping."""
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "quota.sqlite3"
        self.now = [100.0]
        self.limiter = TokenLimiter(self.path, clock=lambda: self.now[0])

    async def asyncTearDown(self):
        """Remove only the test-owned temporary directory."""
        self.folder.cleanup()

    async def test_boundary_expiry_and_tenants(self):
        """Admit the exact budget, reject one extra token, and expire at 60 seconds."""
        await self.limiter.reserve("tenant-a", 49000)
        await self.limiter.reserve("tenant-a", 1000)
        with self.assertRaises(QuotaExceeded) as caught:
            await self.limiter.reserve("tenant-a", 1)
        self.assertEqual(caught.exception.retry_after, 60)
        await self.limiter.reserve("tenant-b", 50000)
        self.now[0] = 159.999
        with self.assertRaises(QuotaExceeded):
            await self.limiter.reserve("tenant-a", 1)
        self.now[0] = 160.0
        await self.limiter.reserve("tenant-a", 50000)

    async def test_concurrent_connections(self):
        """Never over-admit requests racing through separate SQLite connections."""
        results = await asyncio.gather(
            *(self.limiter.reserve("tenant", 1000) for _ in range(80)), return_exceptions=True
        )
        self.assertEqual(sum(not isinstance(result, Exception) for result in results), 50)
        self.assertTrue(
            all(
                isinstance(result, QuotaExceeded)
                for result in results
                if isinstance(result, Exception)
            )
        )

    async def test_persistence_clock_and_settlement(self):
        """Survive reopening, reject backward-clock expiry, and settle once."""
        reservation = await self.limiter.reserve("tenant", 50000)
        reopened = TokenLimiter(self.path, clock=lambda: self.now[0])
        self.now[0] = 50
        with self.assertRaises(QuotaExceeded):
            await reopened.reserve("tenant", 1)
        await reopened.settle(reservation, 100)
        await reopened.settle(reservation, 0)
        await reopened.reserve("tenant", 49900)
        with self.assertRaises(QuotaExceeded):
            await reopened.reserve("tenant", 1)
        for value in (0, -1, True, 50001, "100"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                await reopened.reserve("tenant", value)

    async def test_shared_processes(self):
        """Use independent Python processes, not only asyncio tasks, for admission."""
        code = (
            "import sys; from gateway.limiter import TokenLimiter,QuotaExceeded; "
            "limiter=TokenLimiter(sys.argv[1],limit=5000); "
            "\ntry: limiter._reserve('process-tenant',1000); print('admitted')"
            "\nexcept QuotaExceeded: print('limited')"
        )
        children = [
            await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                code,
                str(self.path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            for _ in range(8)
        ]
        results = await asyncio.gather(*(child.communicate() for child in children))
        self.assertTrue(all(child.returncode == 0 for child in children), results)
        self.assertEqual(sum(stdout.strip() == b"admitted" for stdout, _ in results), 5)


class SlowBody(httpx.AsyncByteStream):
    """Emit frequent chunks that evade inactivity timeouts but not a total deadline."""

    def __init__(self):
        """Track whether timeout cleanup closes the abandoned provider."""
        self.closed = False

    async def __aiter__(self):
        """Continue long enough to exceed the unit test's overall deadline."""
        for _ in range(100):
            await asyncio.sleep(0.01)
            yield b" "

    async def aclose(self):
        """Expose deterministic cancellation cleanup to assertions."""
        self.closed = True


class RouterTests(unittest.IsolatedAsyncioTestCase):
    """Model provider success and failures without any paid inference."""

    async def asyncSetUp(self):
        """Create isolated quota state and configurable provider behavior."""
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "quota.sqlite3"
        self.limiter = TokenLimiter(self.path)
        self.mode = "success"
        self.calls = []
        self.slow = None
        self.body = {
            "choices": [{"message": {"content": "Hello person@example.com"}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10},
        }

        async def provider(request):
            """Make primary behavior explicit while keeping backup deterministic."""
            self.calls.append(request.url.path)
            if request.url.path == "/primary":
                if self.mode in ("429", "both-fail"):
                    return httpx.Response(429, content=b"private upstream secret")
                if self.mode == "500":
                    return httpx.Response(500, content=b"private upstream secret")
                if self.mode == "timeout":
                    await asyncio.sleep(1)
                if self.mode == "trickle":
                    self.slow = SlowBody()
                    return httpx.Response(200, stream=self.slow)
            if request.url.path == "/backup" and self.mode == "both-fail":
                return httpx.Response(500, content=b"private backup secret")
            return httpx.Response(200, json=self.body)

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
        self.router = Router(
            self.client,
            self.limiter,
            "http://provider/primary",
            "http://provider/backup",
            timeout=0.05,
        )
        self.request = CompletionRequest(prompt="demo", max_tokens=100)

    async def asyncTearDown(self):
        """Close HTTP resources before removing the test database."""
        await self.client.aclose()
        self.folder.cleanup()

    def total_charge(self):
        """Inspect persisted budget rather than relying on a returned usage value."""
        database = sqlite3.connect(self.path)
        try:
            return database.execute("SELECT COUNT(*), SUM(tokens) FROM reservations").fetchone()
        finally:
            database.close()

    async def test_success_and_reported_usage(self):
        """Settle known primary usage and apply the guardrail to returned text."""
        result = await self.router.complete("tenant", self.request)
        self.assertEqual(result["model"], "primary")
        self.assertEqual(result["content"], "Hello [REDACTED]")
        self.assertEqual(self.total_charge(), (1, 10))
        self.assertEqual(self.calls, ["/primary"])

    async def test_fallback_causes(self):
        """Fallback on throttling, header delay, and a slowly trickling body."""
        for mode in ("429", "timeout", "trickle"):
            self.mode = mode
            self.calls = []
            result = await self.router.complete(mode, self.request)
            self.assertEqual(result["model"], "backup")
            self.assertEqual(self.calls, ["/primary", "/backup"])
            self.assertEqual(result["usage"]["accounting"], "reserved")
            if self.slow:
                self.assertTrue(self.slow.closed)
        self.assertEqual(self.total_charge(), (3, self.request.reservation_tokens() * 3))

    async def test_failures_and_unknown_usage_stay_charged(self):
        """Do not refund unknown work or retry non-fallback provider errors."""
        self.mode = "500"
        with self.assertRaises(ProviderFailure):
            await self.router.complete("tenant", self.request)
        self.assertEqual(self.calls, ["/primary"])
        self.mode = "both-fail"
        with self.assertRaises(ProviderFailure):
            await self.router.complete("tenant", self.request)
        self.mode = "success"
        self.body.pop("usage")
        result = await self.router.complete("tenant", self.request)
        self.assertEqual(result["usage"]["accounting"], "reserved")
        self.assertEqual(self.total_charge(), (3, self.request.reservation_tokens() * 3))

    async def test_invalid_usage_and_output(self):
        """Reject inconsistent billing metadata rather than undercounting it."""
        for usage in (
            {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 0},
            {"prompt_tokens": 1, "completion_tokens": True, "total_tokens": 2},
            {"prompt_tokens": 1, "completion_tokens": 1000, "total_tokens": 1001},
        ):
            self.body["usage"] = usage
            with self.subTest(usage=usage), self.assertRaises(ProviderFailure):
                await self.router.complete("tenant", self.request)
        self.body = {"choices": [{"message": {"content": "x" * 1000}}]}
        with self.assertRaises(ProviderFailure):
            await self.router.complete("tenant", self.request)

    async def test_cancellation_closes_attempt_without_fallback(self):
        """User cancellation leaves a conservative charge and no second request."""
        self.mode = "trickle"
        task = asyncio.create_task(self.router.complete("tenant", self.request))
        while self.slow is None:
            await asyncio.sleep(0.001)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.slow.closed)
        self.assertEqual(self.calls, ["/primary"])
        self.assertEqual(self.total_charge(), (1, self.request.reservation_tokens()))

    async def test_http_errors_and_quota(self):
        """Expose stable public error bodies and Retry-After through the actual route."""
        app = create_app(
            client=self.client,
            limiter=self.limiter,
            primary="http://provider/primary",
            backup="http://provider/backup",
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            headers = {"Authorization": "Bearer demo-viewer-token"}
            self.mode = "both-fail"
            response = await client.post(
                "/v1/completions", json={"prompt": "demo"}, headers=headers
            )
            self.assertEqual(response.status_code, 502)
            self.assertEqual(
                response.json(),
                {"error": {"code": "provider_unavailable", "message": "Provider unavailable"}},
            )
            await self.limiter.reserve("demo-viewer", 49000)
            response = await client.post(
                "/v1/completions", json={"prompt": "demo", "max_tokens": 1000}, headers=headers
            )
            self.assertEqual(response.status_code, 429)
            self.assertIn("retry-after", response.headers)
            self.assertEqual(len(self.calls), 2)
            response = await client.post(
                "/v1/completions", json={"prompt": "demo", "max_tokens": True}, headers=headers
            )
            self.assertEqual(response.status_code, 400)
