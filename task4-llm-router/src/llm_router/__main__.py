"""Run the router: `python -m llm_router` (default port 8200)."""

import logging

import uvicorn

from llm_router.app import create_app
from llm_router.config import Settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    settings = Settings.from_env()
    logging.getLogger(__name__).info(
        "Primary %s (timeout %.1fs), secondary %s, rate-limit DB %s",
        settings.primary_url,
        settings.primary_timeout_seconds,
        settings.secondary_url,
        settings.db_path,
    )
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
