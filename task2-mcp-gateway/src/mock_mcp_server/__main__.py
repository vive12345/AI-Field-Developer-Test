"""Run the mock downstream server: `python -m mock_mcp_server` (default port 9000)."""

import os

import uvicorn

from mock_mcp_server.app import create_app


def main() -> None:
    uvicorn.run(
        create_app(),
        host=os.getenv("MOCK_MCP_HOST", "127.0.0.1"),
        port=int(os.getenv("MOCK_MCP_PORT", "9000")),
    )


if __name__ == "__main__":
    main()
