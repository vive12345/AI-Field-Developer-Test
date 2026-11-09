"""Run the gateway: `python -m mcp_gateway` (or the `mcp-gateway` script)."""

import logging

import uvicorn

from mcp_gateway.app import create_app
from mcp_gateway.config import Settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    settings = Settings.from_env()
    logging.getLogger(__name__).info("Forwarding authorized requests to %s", settings.downstream_url)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
