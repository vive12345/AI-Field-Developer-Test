# Task 1: Customer MCP Server

An MCP server built with the official Python SDK (`mcp`). It exposes two tools over the **stdio** transport and validates every input strictly with **Pydantic**.

| Tool                  | Inputs                                                  |
| --------------------- | ------------------------------------------------------- |
| `get_customer_record` | `customer_id`: `CUST-XXXXX` (exactly 5 digits)           |
| `trigger_refund`      | `customer_id`, `amount` (positive, ≤ 2 decimals), `reason` (≥ 10 chars after trimming) |

## Quick start

```bash
cd task1-mcp-server
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest -v                  # run the test suite
python -m customer_mcp     # start the server on stdio
```

Try it interactively with the MCP Inspector:

```bash
npx @modelcontextprotocol/inspector .venv/bin/python -m customer_mcp
```

To connect it to Claude Desktop, add this to `claude_desktop_config.json`, using absolute paths:

```json
{
  "mcpServers": {
    "customer-mcp": {
      "command": "/absolute/path/to/task1-mcp-server/.venv/bin/python",
      "args": ["-m", "customer_mcp"]
    }
  }
}
```

## Project layout

```
src/customer_mcp/
├── __main__.py          entry point: starts the stdio transport
├── server.py            tools/list + tools/call (find → validate → execute)
├── schemas.py           Pydantic input models (single source of truth)
├── errors.py            failure → JSON-RPC error / isError result mapping
├── logging_config.py    all logs → stderr
├── store.py             in-memory store seeded from data/
├── data/customers.json  mock customer data
└── tools/
    ├── __init__.py      registry: tool name → schema + handler
    ├── get_customer_record.py
    └── trigger_refund.py
tests/test_server.py     end-to-end tests over real stdio
```

## Design decisions

### 1. stdout carries only JSON-RPC

With stdio, the client parses every stdout line as JSON-RPC, so any stray output breaks the session.

- `logging_config.py` sends every log record to `stderr`.
- The code contains no `print()` calls.
- The tests parse **every** stdout line as JSON and check that the logs appear in stderr.

### 2. How errors are reported

The low-level `Server` is used instead of the high-level helper, so every failure maps to an explicit response:

| Situation                                        | Response                               |
| ------------------------------------------------ | -------------------------------------- |
| Invalid arguments (format, type, range, extras)  | JSON-RPC error `-32602`, with per-field details in `data` |
| Unknown tool name                                | JSON-RPC error `-32602`                 |
| Unknown method                                   | JSON-RPC error `-32601` (SDK)           |
| Valid input, business failure (customer not found, refund over balance) | `result` with `isError: true` |
| Unexpected exception                             | JSON-RPC error `-32603`, generic message; the stack trace goes to stderr only |

In SDK 2.x, an uncaught handler exception is sent over stdio as code `0`, which is not a standard JSON-RPC code. `server.py` therefore catches unexpected exceptions and converts them to `-32603`.

> **Spec note:** recent MCP spec revisions recommend reporting input validation failures as `isError` results so the model can correct itself. This task explicitly requires JSON-RPC error codes, so this server returns `-32602`. The change would be confined to `server.py`.

### 3. Strict validation

- `strict=True`: no type coercion, so `"25"` and `true` are rejected as amounts.
- `extra="forbid"`: unknown fields such as `admin_override` are rejected.
- `customer_id` must match `^CUST-[0-9]{5}$`, so case, length and surrounding whitespace all matter.
- `amount` must be finite and in the range (0, 10 000], with at most 2 decimals. Integers are accepted.
- `reason` must be 10–500 characters after trimming whitespace.

The same models generate the JSON Schema returned by `tools/list`, so the advertised schema and the enforced rules cannot drift apart.

### 4. Mock data

Customers are loaded once at startup from `src/customer_mcp/data/customers.json`. Refunds live in memory: they reduce the customer's balance for the life of the process and never modify the JSON file.

## Production notes

- Use `Decimal` (or integer cents) for money instead of `float`.
- Replace `store.py` with a real database or payment client. The tools and the server would not need to change.
- Add idempotency keys to `trigger_refund` so a retried call cannot refund twice.
