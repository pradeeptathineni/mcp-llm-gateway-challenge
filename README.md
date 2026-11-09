# MCP and LLM Gateway Challenge

A Python reference implementation of validated MCP tools, role-based tool authorization, streaming PII redaction, and resilient model routing.

The examples use synthetic customer records and mock providers. Refunds are simulated. No paid provider or cloud account is required.

## Development

Use Python 3.12 or later and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --locked
make check
```

Each capability includes protocol-level checks and a documented local workflow. See [requirements](docs/requirements.md) for the acceptance map.

## Customer tools

Run `uv run --locked python -m gateway.mcp_server` with an MCP stdio client. [Protocol and validation details](docs/mcp-server.md) explain the synthetic fixtures and error boundaries.
