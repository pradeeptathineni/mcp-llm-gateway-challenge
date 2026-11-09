"""Parse bounded provider SSE events and emit redacted deltas with backpressure."""

import codecs
import json
from collections.abc import AsyncIterator

import httpx

from gateway.http import InvalidPayload, parse_json
from gateway.redaction import Redactor


class StreamFailure(Exception):
    """Represent a provider failure without carrying its raw response."""


async def sse_data(response: httpx.Response, limit: int = 65_536) -> AsyncIterator[str]:
    """Decode UTF-8 across chunk boundaries and cap both lines and complete events."""
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    pending = ""
    data = []
    size = 0
    async for chunk in response.aiter_bytes():
        # HTTP chunks are not SSE frames and may contain many valid events
        for offset in range(0, len(chunk), 1024):
            pending += decoder.decode(chunk[offset : offset + 1024])
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                line = line.removesuffix("\r")
                if len(line.encode()) > limit:
                    raise StreamFailure("Provider line exceeded limit")
                if not line:
                    if data:
                        yield "\n".join(data)
                    data = []
                    size = 0
                else:
                    # Count framing too, so repeated empty data lines remain bounded
                    size += len(line.encode()) + 1
                    if size > limit:
                        raise StreamFailure("Provider event exceeded limit")
                    if line.startswith("data:"):
                        data.append(line[5:].removeprefix(" "))
            if len(pending.encode()) > limit:
                raise StreamFailure("Provider line exceeded limit")
    pending += decoder.decode(b"", final=True)
    if pending or data:
        raise StreamFailure("Truncated provider event")


def event(payload: dict, name: str | None = None) -> bytes:
    """Encode one complete downstream event without raw provider fields."""
    prefix = f"event: {name}\n" if name else ""
    return (prefix + "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode()


async def guarded_stream(
    response: httpx.Response, max_output_bytes: int | None = None
) -> AsyncIterator[bytes]:
    """Close the upstream on completion, errors, cancellation, or client disconnect."""
    redactor = Redactor()
    output_bytes = 0
    try:
        async for data in sse_data(response):
            if data == "[DONE]":
                tail = redactor.finish()
                if tail:
                    yield event({"delta": tail})
                yield event({"done": True}, "done")
                return
            payload = parse_json(data.encode())
            if not isinstance(payload, dict) or "error" in payload:
                raise StreamFailure("Invalid provider event")
            choices = payload.get("choices")
            if not isinstance(choices, list):
                raise StreamFailure("Invalid provider choices")
            if not choices:
                continue
            choice = choices[0]
            if not isinstance(choice, dict) or not isinstance(choice.get("delta"), dict):
                raise StreamFailure("Invalid provider delta")
            content = choice["delta"].get("content", "")
            if content is None:
                continue
            if not isinstance(content, str):
                raise StreamFailure("Invalid provider content")
            output_bytes += len(content.encode())
            if max_output_bytes is not None and output_bytes > max_output_bytes:
                raise StreamFailure("Provider output exceeded reservation")
            safe = redactor.feed(content)
            if safe:
                yield event({"delta": safe})
        raise StreamFailure("Provider stream ended without completion")
    except (httpx.HTTPError, UnicodeError, InvalidPayload, StreamFailure):
        yield event(
            {"error": {"code": "upstream_stream_error", "message": "Provider stream failed"}},
            "error",
        )
    finally:
        await response.aclose()
