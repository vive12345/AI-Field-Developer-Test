"""Logging setup.

With the stdio transport, stdout is reserved for JSON-RPC messages.
Every log line must therefore go to stderr - never use print() in this project.
"""

import logging
import sys

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def setup_logging(level: int = logging.INFO) -> None:
    """Send all log output to stderr."""
    logging.basicConfig(level=level, format=LOG_FORMAT, stream=sys.stderr)
