"""Entry point: `python -m customer_mcp` (or the `customer-mcp` script)."""

import asyncio
import logging

from mcp.server.stdio import stdio_server

from customer_mcp.logging_config import setup_logging
from customer_mcp.server import create_server

logger = logging.getLogger("customer_mcp")


async def run() -> None:
    server = create_server()
    async with stdio_server() as (read_stream, write_stream):
        logger.info("Customer MCP server started on stdio")
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    setup_logging()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("Customer MCP server stopped")


if __name__ == "__main__":
    main()
