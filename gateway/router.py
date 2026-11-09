"""Enforce one tenant admission and bounded, cancellation-safe model failover."""

import asyncio
from dataclasses import dataclass
from typing import Annotated

import httpx
from pydantic import BaseModel, ConfigDict, Field

from gateway.http import InvalidPayload, parse_json
from gateway.limiter import TokenLimiter
from gateway.redaction import Redactor


class CompletionRequest(BaseModel):
    """Bound the request and reserve output before contacting either model."""

    model_config = ConfigDict(strict=True, extra="forbid")
    prompt: Annotated[str, Field(min_length=1, max_length=4096)]
    max_tokens: Annotated[int, Field(ge=1, le=4096)] = 256

    def reservation_tokens(self) -> int:
        """Use a conservative UTF-8 byte budget plus framing and requested output."""
        return len(self.prompt.encode()) + 32 + self.max_tokens


class ProviderFailure(Exception):
    """Carry a stable category rather than upstream messages or exception text."""

    def __init__(self, retryable=False):
        """Allow fallback only for the explicitly defined primary failures."""
        self.retryable = retryable
        super().__init__("Provider unavailable")


@dataclass(frozen=True)
class ProviderResult:
    """Accept only the content and validated usage needed by the caller."""

    content: str
    tokens: int | None


class Router:
    """Apply a total request deadline, including headers and body consumption."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        limiter: TokenLimiter,
        primary: str,
        backup: str,
        timeout=3.0,
    ):
        """Use a fixed trusted provider pair and independent attempt deadlines."""
        self.client = client
        self.limiter = limiter
        self.primary = primary
        self.backup = backup
        self.timeout = timeout

    async def _attempt(self, url: str, request: CompletionRequest) -> ProviderResult:
        """Cancel and close a failed attempt before another provider is contacted."""
        try:
            async with asyncio.timeout(self.timeout):
                async with self.client.stream("POST", url, json=request.model_dump()) as response:
                    if response.status_code == 429:
                        raise ProviderFailure(retryable=True)
                    if response.status_code != 200:
                        raise ProviderFailure()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > 1_048_576:
                            raise ProviderFailure()
                        body.extend(chunk)
                    payload = parse_json(bytes(body))
                    content = payload["choices"][0]["message"]["content"]
                    if not isinstance(content, str):
                        raise ProviderFailure()
                    usage = payload.get("usage")
                    tokens = None
                    if usage is not None:
                        prompt = usage["prompt_tokens"]
                        completion = usage["completion_tokens"]
                        tokens = usage["total_tokens"]
                        if (
                            any(
                                type(value) is not int or value < 0
                                for value in (prompt, completion, tokens)
                            )
                            or prompt + completion != tokens
                            or completion > request.max_tokens
                            or tokens > request.reservation_tokens()
                        ):
                            raise ProviderFailure()
                    if tokens is None and len(content.encode()) > request.max_tokens:
                        raise ProviderFailure()
                    return ProviderResult(content, tokens)
        except (TimeoutError, httpx.TimeoutException):
            raise ProviderFailure(retryable=True) from None
        except (httpx.HTTPError, InvalidPayload, KeyError, TypeError, IndexError):
            raise ProviderFailure() from None

    async def complete(self, tenant: str, request: CompletionRequest) -> dict:
        """Count fallback once against tenant quota, retaining unknown attempt usage."""
        reservation = await self.limiter.reserve(tenant, request.reservation_tokens())
        selected = "primary"
        try:
            result = await self._attempt(self.primary, request)
        except ProviderFailure as failure:
            if not failure.retryable:
                raise
            selected = "backup"
            result = await self._attempt(self.backup, request)
        # A timeout may still incur upstream usage, so fallback keeps the full reservation
        charged = reservation.tokens
        accounting = "reserved"
        if result.tokens is not None and selected == "primary":
            await self.limiter.settle(reservation, result.tokens)
            charged = result.tokens
            accounting = "reported"
        redactor = Redactor()
        safe_content = redactor.feed(result.content) + redactor.finish()
        return {
            "model": selected,
            "content": safe_content,
            "usage": {"total_tokens": charged, "accounting": accounting},
        }
