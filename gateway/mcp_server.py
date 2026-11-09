"""Expose validated synthetic customer tools over the official MCP stdio transport."""

import asyncio
import json
import logging
import sys
from typing import Annotated
from uuid import uuid4

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

# Schema and runtime validation share one strict definition
CustomerId = Annotated[str, Field(pattern=r"^CUST-[0-9]{5}$", min_length=10, max_length=10)]


class CustomerInput(BaseModel):
    """Reject coercion and undeclared arguments before tool execution."""

    model_config = ConfigDict(strict=True, extra="forbid")
    customer_id: CustomerId


class RefundInput(CustomerInput):
    """Accept only finite positive amounts and a meaningful reason."""

    amount: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    reason: Annotated[str, Field(min_length=10, max_length=1000, pattern=r"\S.{8,}\S")]


# Fixtures contain no real customer information or payment connection
CUSTOMERS = {
    "CUST-00001": {"customer_id": "CUST-00001", "name": "Example Customer", "tier": "standard"},
    "CUST-00002": {"customer_id": "CUST-00002", "name": "Sample Customer", "tier": "premium"},
}
SCHEMAS = {"get_customer_record": CustomerInput, "trigger_refund": RefundInput}
server = Server("customer-tools", version="0.1.0")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    """Publish exactly the schemas enforced by the tool dispatcher."""
    return [
        types.Tool(
            name="get_customer_record",
            description="Look up a synthetic customer record.",
            inputSchema=CustomerInput.model_json_schema(),
        ),
        types.Tool(
            name="trigger_refund",
            description="Simulate a refund. No funds are moved.",
            inputSchema=RefundInput.model_json_schema(),
        ),
    ]


async def call_tool(request: types.CallToolRequest) -> types.ServerResult:
    """Keep schema failures as JSON-RPC errors rather than tool execution results."""
    name = request.params.name
    model = SCHEMAS.get(name)
    if model is None:
        raise McpError(types.ErrorData(code=-32602, message="Unknown tool"))
    try:
        arguments = model.model_validate(request.params.arguments or {})
    except ValidationError as error:
        # Exclude input values so invalid data is not reflected or logged
        fields = sorted({str(item["loc"][0]) for item in error.errors() if item["loc"]})
        raise McpError(
            types.ErrorData(code=-32602, message="Invalid tool arguments", data={"fields": fields})
        ) from None

    customer = CUSTOMERS.get(arguments.customer_id)
    if customer is None:
        return types.ServerResult(
            types.CallToolResult(
                content=[types.TextContent(type="text", text="Customer not found")], isError=True
            )
        )
    if isinstance(arguments, RefundInput):
        result = {
            "refund_id": f"SIM-{uuid4().hex}",
            "customer_id": arguments.customer_id,
            "amount": arguments.amount,
            "status": "simulated",
        }
    else:
        result = dict(customer)
    return types.ServerResult(
        types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(result))],
            structuredContent=result,
            isError=False,
        )
    )


# The SDK's call_tool decorator converts exceptions into isError results
# Register the typed handler directly to preserve the required -32602 wire error
server.request_handlers[types.CallToolRequest] = call_tool


async def run() -> None:
    """Leave stdout exclusively owned by the SDK's JSON-RPC writer."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    logging.getLogger(__name__).info("Starting synthetic customer MCP server")
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
