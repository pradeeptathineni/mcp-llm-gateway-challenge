# Streaming PII guardrail

With the mock provider from the proxy example running on port 8011, start:

```sh
uv run --locked uvicorn gateway.llm_gateway:app --host 127.0.0.1 --port 8012
curl -N http://127.0.0.1:8012/v1/stream \
  -H 'Authorization: Bearer demo-viewer-token' \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Show the synthetic example"}'
```

The gateway accepts OpenAI-style SSE deltas from `LLM_STREAM_URL` and emits normalized SSE events containing `delta`, followed by a `done` event. It never returns provider metadata or raw error bodies. The mock produces split email, SSN, and card patterns without calling a paid model.

## Buffering and latency

The redactor retains one unfinished lexical candidate, at most 512 characters. Ordinary words are released at their next safe delimiter. Numeric candidates may include whitespace or hyphens. A candidate over the limit emits one `[REDACTED]` marker and is suppressed until its boundary. This deliberately prefers over-redaction to leaking a long email prefix.

The stream also stops if raw content exceeds the conservative UTF-8 output-byte allowance (`max_tokens`, default 256). This is stricter than provider tokens, but does not trust a provider to honor its requested output bound.

There is no whole-response buffer. Provider events are limited to 64 KiB, and UTF-8 decoding preserves characters split across byte chunks. The asynchronous iterator follows downstream demand rather than consuming the provider in a background task. Completion, failures, and consumer cancellation close the upstream response.

Immediate emission of every input character cannot guarantee cross-chunk redaction. A partial `person@` must remain withheld until it is safe to classify. The design trades at most one bounded candidate's delay for that guarantee. The real-socket test checks that ordinary text reaches the client before the provider finishes.

## Recognition scope

- ASCII email addresses with a dotted domain.
- US SSN-shaped values containing three, two, and four digits separated by hyphens.
- 13–19 digit card-shaped values, including spaces, tabs, newlines, and hyphens. Luhn validation is deliberately not used to avoid missing sensitive-looking numbers.

This is deterministic pattern redaction, not universal PII detection. Internationalized or obfuscated email addresses and context-specific identifiers need additional policy. Long non-PII tokens and ordinary long numbers can be over-redacted. Limits apply before classification, so no unfinished prefix is released to reduce that false-positive rate.

A provider failure after headers produces an `error` SSE event, not an HTTP status change. Pending candidate text is discarded on failure. Missing `[DONE]`, malformed JSON, invalid UTF-8, and oversized events fail closed. A completed stream includes a `done` event; clients must distinguish it from a connection ending early.

## Verification

`make check` covers every two-way split of representative patterns, character-at-a-time input, EOF, adjacent patterns, UTF-8 byte splits, overlong candidates, repeated safe text, early close, truncated streams, sanitized errors, and real HTTP streaming. These checks establish the declared grammar, not a universal DLP guarantee.

## Sources

- [SSE event stream format](https://html.spec.whatwg.org/multipage/server-sent-events.html#event-stream-interpretation)
- [HTTPX streaming responses](https://www.python-httpx.org/async/#streaming-responses)
