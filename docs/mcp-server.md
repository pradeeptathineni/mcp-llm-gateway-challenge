# Validated customer tools

Run `uv run --locked python -m gateway.mcp_server`. Connect with an MCP client over stdin/stdout. Diagnostics go to stderr. The process has no HTTP listener and no refund/payment integration.

The server publishes `get_customer_record` and `trigger_refund`. Customer IDs contain exactly five ASCII digits after `CUST-`. The two records are `CUST-00001` and `CUST-00002`. Refund amounts must be finite positive JSON numbers. Reasons contain at least ten meaningful characters. Undeclared fields and type coercion are rejected.

Invalid tool arguments and unknown tool names return `-32602` with the original request ID. Validly formed calls for missing customers return an MCP tool result with `isError: true`. Validation errors list field names without echoing values.

## Protocol choice

The locked official Python SDK is `mcp==1.30.0`, using its initialization handshake and supported protocol versions. The wire test negotiates `2025-11-25`. This is not an implementation of the newer stateless MCP revision.

The SDK's convenience tool decorator converts all exceptions to tool-error results. A typed request handler preserves the assignment's required JSON-RPC argument errors. Transport framing and session management remain SDK-owned. Unknown protocol methods follow the pinned SDK's `-32602` validation behavior. Malformed JSON transport handling is SDK-owned and is not claimed as a custom `-32700` implementation.

## Checks

`make check` validates strict schemas, both tools, missing customers, original numeric request IDs, unknown tool names, stdout isolation, clean EOF, and interoperability with the official SDK client. Tests spawn the actual server process.

## Sources

- [Official Python SDK v1.30.0](https://github.com/modelcontextprotocol/python-sdk/tree/v1.30.0)
- [MCP tools and error handling](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
- [Pydantic strict mode](https://docs.pydantic.dev/latest/concepts/strict_mode/)
