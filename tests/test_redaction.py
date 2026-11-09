"""Verify redaction independently of provider chunk boundaries."""

import json
import unittest

import httpx

from gateway.redaction import Redactor
from gateway.streaming import guarded_stream


class RedactionTests(unittest.TestCase):
    """Check every split, one-character chunks, EOF, and adversarial candidates."""

    def test_every_split(self):
        """Produce identical safe text at every possible two-chunk boundary."""
        cases = (
            ("Order 12 12name@example.com", "Order 12 [REDACTED]"),
            ("card=4111 1111 1111 1111", "card=[REDACTED]"),
            ("$4111 1111 1111 1111", "$[REDACTED]"),
            ("Mail person@example.com now", "Mail [REDACTED] now"),
            ("SSN 123-45-6789", "SSN [REDACTED]"),
            ("Card 4111 1111 1111 1111.", "Card [REDACTED]."),
            ("person@example.com,4111111111111111;123-45-6789", "[REDACTED],[REDACTED];[REDACTED]"),
            ("12name+tag@example.co.uk", "[REDACTED]"),
            ("Hello café 世界 👋. Order 123 costs 42.", "Hello café 世界 👋. Order 123 costs 42."),
        )
        for text, expected in cases:
            for split in range(len(text) + 1):
                redactor = Redactor()
                with self.subTest(text=text, split=split):
                    result = (
                        redactor.feed(text[:split])
                        + redactor.feed(text[split:])
                        + redactor.finish()
                    )
                    self.assertEqual(result, expected)
            redactor = Redactor()
            self.assertEqual(
                "".join(redactor.feed(char) for char in text) + redactor.finish(), expected
            )

    def test_bounded_and_fail_closed(self):
        """Do not leak any prefix when an unfinished candidate exceeds memory bounds."""
        for text in ("a" * 10000 + "@example.com", "4" * 10000, "4 " * 10000):
            redactor = Redactor()
            output = "".join(redactor.feed(text[i : i + 7]) for i in range(0, len(text), 7))
            self.assertEqual(output + redactor.finish(), "[REDACTED]")
            self.assertLessEqual(redactor.max_buffered, 512)
        redactor = Redactor()
        for _ in range(20000):
            self.assertEqual(redactor.feed("safe words "), "safe words ")
        self.assertLessEqual(redactor.max_buffered, 5)

    def test_safe_prefix_before_eof(self):
        """Release ordinary prose while a later sensitive token remains withheld."""
        redactor = Redactor()
        self.assertEqual(redactor.feed("Hello "), "Hello ")
        self.assertEqual(redactor.feed("person@"), "")
        self.assertEqual(redactor.feed("example.com"), "")
        self.assertEqual(redactor.finish(), "[REDACTED]")


class BytesStream(httpx.AsyncByteStream):
    """Expose arbitrary network boundaries and track resource cleanup."""

    def __init__(self, chunks):
        """Keep fixture bytes separate from the parser under test."""
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        """Yield fixture bytes without combining UTF-8 or SSE boundaries."""
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        """Record both normal and early stream closure."""
        self.closed = True


class StreamTests(unittest.IsolatedAsyncioTestCase):
    """Use real HTTPX response streaming with synthetic provider frames."""

    async def test_utf8_one_byte_and_split_pii(self):
        """Decode multibyte characters and redact content split over provider deltas."""
        data = b""
        for text in ("Hello café 👋. ", "person@", "example.com", " 123-45-", "6789"):
            data += (
                "data: "
                + json.dumps({"choices": [{"delta": {"content": text}}]}, ensure_ascii=False)
                + "\r\n\r\n"
            ).encode()
        data += b"data: [DONE]\n\n"
        stream = BytesStream([bytes([byte]) for byte in data])
        response = httpx.Response(200, stream=stream)
        events = [chunk async for chunk in guarded_stream(response)]
        parsed = [
            json.loads(line[6:])
            for chunk in events
            for line in chunk.decode().splitlines()
            if line.startswith("data: ")
        ]
        self.assertEqual(
            "".join(p.get("delta", "") for p in parsed), "Hello café 👋. [REDACTED] [REDACTED]"
        )
        self.assertTrue(parsed[-1]["done"])
        self.assertTrue(stream.closed)

    async def test_failures_are_sanitized(self):
        """Withhold pending candidate text and return only a stable stream error."""
        for data in (
            b'data: {"error":"SECRET"}\n\n',
            b"data: broken SECRET\n\n",
            b"data: " + b"x" * 70000,
            b'data: {"choices":[]}\n\n',
            b"data: \xff\n\n",
        ):
            stream = BytesStream([data])
            result = b"".join(
                [chunk async for chunk in guarded_stream(httpx.Response(200, stream=stream))]
            )
            self.assertIn(b"upstream_stream_error", result)
            self.assertNotIn(b"SECRET", result)
            self.assertNotIn(b'"done": true', result)
            self.assertTrue(stream.closed)

    async def test_early_close_cleans_upstream(self):
        """A downstream consumer stopping early does not leave the provider open."""
        data = b'data: {"choices":[{"delta":{"content":"Hello "}}]}\n\n'
        stream = BytesStream([data, b"data: [DONE]\n\n"])
        generator = guarded_stream(httpx.Response(200, stream=stream))
        self.assertIn(b"Hello", await anext(generator))
        await generator.aclose()
        self.assertTrue(stream.closed)

    async def test_coalesced_events_are_individually_bounded(self):
        """Accept many small events even if one network chunk exceeds the event cap."""
        frame = b'data: {"choices":[{"delta":{"content":"safe "}}]}\n\n'
        data = frame * 2000 + b"data: [DONE]\n\n"
        self.assertGreater(len(data), 65536)
        stream = BytesStream([data])
        output = b"".join(
            [item async for item in guarded_stream(httpx.Response(200, stream=stream))]
        )
        self.assertNotIn(b"upstream_stream_error", output)
        self.assertIn(b'"done": true', output)

    async def test_output_budget_closes_overproducing_stream(self):
        """A provider ignoring max_tokens cannot escape its conservative reservation."""
        frame = b'data: {"choices":[{"delta":{"content":"safe "}}]}\n\n'
        stream = BytesStream([frame] * 100)
        output = b"".join(
            [
                item
                async for item in guarded_stream(
                    httpx.Response(200, stream=stream), max_output_bytes=10
                )
            ]
        )
        self.assertIn(b"upstream_stream_error", output)
        self.assertNotIn(b'"done": true', output)
        self.assertEqual(output.count(b'"delta"'), 2)
        self.assertTrue(stream.closed)

    async def test_empty_data_lines_are_bounded(self):
        """Count framing even when every data field contains an empty string."""
        stream = BytesStream([b"data:\n" * 20000])
        result = b"".join(
            [item async for item in guarded_stream(httpx.Response(200, stream=stream))]
        )
        self.assertIn(b"upstream_stream_error", result)
        self.assertTrue(stream.closed)
