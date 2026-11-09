"""Minimal Server-Sent Events (SSE) parsing and formatting.

LLM providers stream responses as SSE: each event is one or more `data:` lines
followed by a blank line. This parser works line by line, so it never needs
more than the current event in memory.
"""

from collections.abc import AsyncIterator


async def iter_sse_data(lines: AsyncIterator[str]) -> AsyncIterator[str]:
    """Yield the `data` payload of each SSE event as soon as the event is complete."""
    data_lines: list[str] = []

    async for line in lines:
        if line == "":  # blank line = end of event
            if data_lines:
                yield "\n".join(data_lines)
                data_lines = []
            continue

        if line.startswith(":"):  # comment / keep-alive
            continue

        field, _, value = line.partition(":")
        if field == "data":
            data_lines.append(value.removeprefix(" "))

    if data_lines:  # stream ended without a trailing blank line
        yield "\n".join(data_lines)


def format_sse(data: str) -> bytes:
    return f"data: {data}\n\n".encode()
