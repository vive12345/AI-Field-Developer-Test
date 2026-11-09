"""Run the mock provider: `python -m mock_llm_provider` (default port 9100)."""

import os

import uvicorn

from mock_llm_provider.app import create_app


def main() -> None:
    delay_ms = float(os.getenv("MOCK_LLM_CHUNK_DELAY_MS", "40"))
    uvicorn.run(
        create_app(chunk_delay_seconds=delay_ms / 1000),
        host=os.getenv("MOCK_LLM_HOST", "127.0.0.1"),
        port=int(os.getenv("MOCK_LLM_PORT", "9100")),
    )


if __name__ == "__main__":
    main()
