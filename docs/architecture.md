# Architecture and boundaries

The source keeps protocol, policy, streaming, and accounting separate.

- `mcp_server.py` owns the official-SDK stdio session and synthetic customer tools.
- `mcp_proxy.py` authenticates the caller and authorizes `admin_*` before transparent downstream forwarding.
- `llm_gateway.py` validates HTTP input and owns the application/client lifecycle.
- `streaming.py` parses bounded SSE events. `redaction.py` classifies complete bounded candidates.
- `router.py` owns the completion deadline and one fallback attempt. `limiter.py` owns SQLite admission and settlement.
- `mock_server.py` is the deterministic upstream used in local examples and TCP integration tests.

The streaming and completion endpoints share tenant admission. The MCP tool proxy is independent because its requests are tool calls, not model tokens. Demo authentication is an opaque-token lookup; the caller cannot nominate its role or upstream destination.

## Security scope

This repository is a runnable local assessment, not a production identity or payment system. Bind it to `127.0.0.1`. Public fixture tokens are examples and provide no security for an internet-facing deployment. Refunds are simulated and all records and PII patterns used by tests are synthetic.

A production rollout needs an approved identity provider, TLS and network policy, real provider adapters, model-specific token accounting, request/concurrency limits, privacy-aware observability, and an explicit data retention policy. Those are deployment decisions rather than hidden defaults here. No credentials or provider accounts are needed to run the repository.

The token window is shared only by processes using the same local SQLite file. It is not a multi-host global quota. Provider billing and tenant admission are distinct. Streaming redaction recognizes its declared grammar and deliberately suppresses oversized candidates rather than releasing uncertain prefixes.

## Operations

Use Python 3.12 or 3.13 on macOS or Linux. The lockfile includes both supported platforms. `uv sync --locked` installs dependencies, and `make check` runs formatting, lint, unit, protocol, concurrency, and local TCP checks. The tests reserve ephemeral loopback sockets and stop the child processes they started.

Documented service ports are 8010 for the MCP proxy, 8011 for mocks, and 8012 for the LLM gateway. Stop foreground services with Ctrl-C. Keep the SQLite file and its sidecars outside version control. Restarting the gateway preserves the active quota window.

`make secret-check` requires Gitleaks and scans committed history. Its only repository-specific exclusion is the exact public demo bearer syntax in the three walkthroughs. This does not exempt source files or arbitrary authorization headers.

The included GitHub workflow runs `make check` on Linux and macOS. It is configured, but its first hosted execution remains separate from local verification.
