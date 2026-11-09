# MCP security proxy

Start two terminals from the repository root:

```sh
uv run --locked uvicorn gateway.mock_server:app --host 127.0.0.1 --port 8011
uv run --locked uvicorn gateway.mcp_proxy:app --host 127.0.0.1 --port 8010
```

Call the proxy:

```sh
curl http://127.0.0.1:8010/mcp \
  -H 'Authorization: Bearer demo-viewer-token' \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

The public demo tokens map to server-controlled identities. `demo-admin-token` has the admin role. No role field from the request is trusted. These tokens are not suitable for a public listener. Production authentication must verify issuer, audience, expiry, revocation, and tenant membership through an approved identity provider.

## Policy and forwarding

- `tools/list` is forwarded unchanged, including cursor, metadata, and the unfiltered response. Discovery is not authorization.
- `tools/call` names starting with `admin_` require the admin role. Viewer calls return `-32001: Unauthorized Tool Call` with the original ID and make no downstream request.
- Authorized requests preserve their body bytes. Caller bearer credentials are not sent downstream.
- One request is accepted per HTTP body. Batches are rejected to avoid ambiguous authorization. Valid notifications have no JSON-RPC response body; a denied notification gets HTTP 403 and never reaches upstream.
- Requests are bounded to 64 KiB and responses to 1 MiB. Duplicate JSON keys, invalid envelopes, and nonstandard numeric constants are rejected.
- Errors do not expose upstream bodies, headers, or exception text. Redirects and proxy environment configuration are disabled.

`MCP_DOWNSTREAM_URL` selects the trusted upstream at process startup. It is not accepted from callers. This is an HTTP JSON-RPC assessment proxy, not a complete MCP Streamable HTTP session implementation.

## Verification

`make check` tests authentication failures, role forgery, transparent byte forwarding, notifications, batch rejection, numeric and string IDs, bounded parsing, upstream failures, and real loopback HTTP between separate processes.

## Sources

- [JSON-RPC 2.0](https://www.jsonrpc.org/specification)
- [MCP authorization security considerations](https://modelcontextprotocol.io/specification/2025-11-25/basic/security_best_practices)
