# Task 2: MCP Security Gateway

A lightweight HTTP/JSON-RPC reverse proxy that sits between an AI agent and a downstream MCP server. It enforces role-based access to tools before any request is forwarded.

```
AI agent ──HTTP + JSON-RPC──▶  Gateway (:8000)  ──allowed──▶  Mock MCP server (:9000)
          Authorization:       1. authenticate                  executes the tool
          Bearer <token>       2. parse JSON-RPC
                               3. authorize ──denied──▶ -32001, never forwarded
```

## Quick start

```bash
cd task2-mcp-gateway
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest -v                       # run the test suite

python -m mock_mcp_server       # terminal 1: downstream on :9000
python -m mcp_gateway           # terminal 2: gateway on :8000
```

Try it:

```bash
# Viewer calling an admin tool → 403, JSON-RPC error -32001
curl -s localhost:8000/mcp -H "Authorization: Bearer viewer-token-bob" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{"key_id":"KEY-123"}}}'

# Admin calling the same tool → forwarded, 200
curl -s localhost:8000/mcp -H "Authorization: Bearer admin-token-alice" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{"key_id":"KEY-123"}}}'
```

Mock tokens are defined in [`src/mcp_gateway/data/tokens.json`](src/mcp_gateway/data/tokens.json):

| Token                | User  | Role   |
| -------------------- | ----- | ------ |
| `admin-token-alice`  | alice | admin  |
| `viewer-token-bob`   | bob   | viewer |
| `viewer-token-carol` | carol | viewer |

## Configuration

| Variable                     | Default                       |
| ---------------------------- | ----------------------------- |
| `GATEWAY_DOWNSTREAM_URL`     | `http://127.0.0.1:9000/mcp`   |
| `GATEWAY_DOWNSTREAM_TIMEOUT` | `10` (seconds)                |
| `GATEWAY_HOST` / `GATEWAY_PORT` | `127.0.0.1` / `8000`       |
| `MOCK_MCP_HOST` / `MOCK_MCP_PORT` | `127.0.0.1` / `9000`     |

## Project layout

```
src/mcp_gateway/
├── app.py         POST /mcp pipeline: authenticate → parse → authorize → forward
├── auth.py        Bearer token → Identity(user, role)
├── policy.py      authorization rule: admin_ tools require the admin role
├── jsonrpc.py     JSON-RPC parsing, validation and error codes
├── proxy.py       streams authorized requests to the downstream server
├── config.py      environment-based settings
└── data/tokens.json
src/mock_mcp_server/
├── app.py         JSON-RPC endpoint (initialize, tools/list, tools/call)
├── tools.py       mock tool implementations
└── data/          tools.json, customers.json, orders.json
tests/test_gateway.py
```

## Behaviour

| Situation                                      | HTTP | JSON-RPC error | Forwarded? |
| ---------------------------------------------- | ---- | -------------- | ---------- |
| `tools/list`, any valid token                  | 200  | –              | ✅          |
| `tools/call`, non-admin tool                   | 200  | –              | ✅          |
| `tools/call`, `admin_*` tool, admin role       | 200  | –              | ✅          |
| `tools/call`, `admin_*` tool, viewer role      | 403  | `-32001` Unauthorized Tool Call | ❌ |
| Missing / malformed / unknown token            | 401  | `-32000` (+ `WWW-Authenticate: Bearer`) | ❌ |
| Body is not valid JSON                         | 400  | `-32700` Parse error | ❌     |
| Invalid envelope, batch array, duplicate keys  | 400  | `-32600` Invalid Request | ❌ |
| `tools/call` without a string `params.name`    | 400  | `-32602` Invalid params | ❌  |
| Downstream unreachable / timed out             | 502 / 504 | `-32002` | –            |

Downstream responses, including the downstream server's own JSON-RPC errors, are passed back unchanged.

## Design decisions

- **Order of checks: authenticate before parsing.** An unauthenticated caller can't use the JSON parser as an attack surface, and every rejection happens before the downstream server is contacted. The tests prove this: the mock server records every message it receives, and each denial test asserts that the record is empty.
- **Closing smuggling routes.**
  - *Batches:* JSON-RPC batches are rejected outright. MCP removed batching in the 2025-06-18 spec, and rejecting them also stops an admin call from being hidden inside an allowed batch.
  - *Duplicate keys:* JSON objects with duplicate keys are rejected, e.g. `{"name": "get_customer", "name": "admin_reset_key"}`. Different parsers keep different duplicates, so without this check the gateway and the downstream server could disagree about which tool is being called.
  - *Name normalisation:* the `admin_` check ignores case and surrounding whitespace, so `ADMIN_reset_key` and `" admin_reset_key"` are blocked too.
  - *Re-serialization:* the gateway forwards the parsed, re-serialized message instead of the raw bytes, so the downstream server receives exactly what was authorized.
- **No credential forwarding.** The client's `Authorization` header stays at the gateway. The downstream server receives the verified identity as `X-Gateway-User` and `X-Gateway-Role` instead.
- **Streaming pass-through.** Responses are streamed back as they arrive, so plain JSON responses and SSE responses both work.
- **HTTP status codes.** A denial returns **403** with the JSON-RPC error body. Status-code-based monitoring sees the denial, and JSON-RPC clients still get a standard error object they can parse.
- **Audit logging.** Every denied call is logged with the user, role and tool name, e.g. `DENIED tools/call ('admin_reset_key') for user=bob role=viewer`.

## Production notes

- Replace the static token file with JWT verification (signature, expiry, audience) or token introspection. Only `auth.py` would change.
- Consider filtering `admin_*` tools out of `tools/list` for viewers, so the model never sees tools it can't use. The task requires transparent forwarding, so this server doesn't filter.
- Add a request body size limit and rate limiting at the gateway.
- Support `GET /mcp` (server-initiated SSE) and `DELETE /mcp` (session termination) for full Streamable HTTP coverage.
