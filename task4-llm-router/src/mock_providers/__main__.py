"""Run a mock provider.

    MOCK_PROVIDER_NAME=primary   MOCK_PROVIDER_PORT=9201 python -m mock_providers
    MOCK_PROVIDER_NAME=secondary MOCK_PROVIDER_PORT=9202 python -m mock_providers
"""

import os

import uvicorn

from mock_providers.app import create_app


def main() -> None:
    app = create_app(
        name=os.getenv("MOCK_PROVIDER_NAME", "primary"),
        mode=os.getenv("MOCK_PROVIDER_MODE", "ok"),
        slow_seconds=float(os.getenv("MOCK_PROVIDER_SLOW_SECONDS", "5")),
    )
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("MOCK_PROVIDER_PORT", "9201")))


if __name__ == "__main__":
    main()
