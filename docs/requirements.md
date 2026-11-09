# Requirements and acceptance

The solution covers four independent capabilities and their integration.

1. An official-SDK stdio MCP server exposes `get_customer_record` and `trigger_refund`, validates every argument strictly, returns JSON-RPC errors for invalid parameters, and keeps stdout protocol-only.
2. An HTTP JSON-RPC gateway authenticates bearer tokens, forwards `tools/list`, and denies non-admin `admin_*` calls with `-32001` before any downstream request.
3. A streaming gateway replaces email, SSN, and payment-card patterns with `[REDACTED]`, including patterns split across chunks, using bounded buffering.
4. A tenant-specific sliding token window uses on-disk SQLite. The router falls back on primary HTTP 429 or a 3000 ms timeout and returns sanitized errors.

## Boundaries

The implementation is an assessment-sized service. Mock data and providers make its behavior reproducible without customer information, live refunds, paid inference, or cloud resources. Deployment and distributed operation need separate threat modeling and operational review.
