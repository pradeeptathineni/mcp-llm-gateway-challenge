# Verification record

Verified locally on November 9, 2026, on macOS with Python 3.12.10. Dependencies were installed from the locked graph. No AWS resources, paid model calls, live customer data, or real refunds were used.

## Passed locally

- Ruff lint and formatting checks.
- 34 unittest methods, including subcases for every split point of representative PII patterns.
- Official SDK MCP initialization, discovery, successful tools, invalid schema errors, original request IDs, stdout isolation, and stderr diagnostics.
- MCP proxy tests for transparent metadata/pagination, admin denial before downstream I/O, forged roles, duplicate auth headers, notifications, batches, malformed JSON, and sanitized failures.
- Streaming checks for UTF-8 byte splits, attached-prefix card values, numeric-leading emails, EOF, long candidates, empty SSE fields, coalesced events, overproduction, early closure, and errors.
- Exact SQLite quota boundaries, expiry, tenant isolation, persistence, settlement, backward clock changes, 80 concurrent admissions, and eight independent processes.
- Completion fallback for HTTP 429, header timeout, and trickling-body timeout, with conservative charging and cancellation cleanup.
- Separate real HTTP processes for proxying, streaming before provider completion, output caps, and the default 3000 ms primary deadline.

Review found and corrected candidate-boundary leaks, unbounded empty SSE fields, network-chunk/event confusion, and a missing streaming output cap. Regression checks cover those findings.

## Verification limits

Hosted CI, Linux execution, Python 3.13 execution, public deployment, live model adapters, and cloud deployment were not run locally. The documentation describes protocol and pattern boundaries instead of claiming general MCP revision compliance or universal PII detection. The pinned MCP SDK owns malformed stdio transport behavior.

This record describes the tested implementation. Passing checks do not establish production readiness or acceptance by an external evaluator.
