"""Run the gateway: `python -m llm_gateway` (default port 8100)."""

import logging

import uvicorn

from llm_gateway.app import create_app
from llm_gateway.config import Settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    settings = Settings.from_env()
    logging.getLogger(__name__).info("Routing completions to %s", settings.upstream_url)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
