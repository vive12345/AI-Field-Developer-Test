# Task 4: Rate-Limiting & Model Fallback Router

A resilient routing module for an LLM gateway. It provides:
- a **token-aware sliding-window rate limiter** per tenant API key, stored in an on-disk **SQLite** file
- automatic **failover** from the primary to a secondary model provider when the primary returns 429, times out after **3000 ms**, is unreachable, or returns a 5xx error
- a **standardized error payload** that never leaks upstream bodies, stack traces or internals

```
client ─▶ router (:8200)
            1. tenant API key        ─▶ 401 invalid_api_key
            2. validate + estimate   ─▶ 400 invalid_request / request_too_large
            3. reserve tokens (SQLite) ─▶ 429 rate_limit_exceeded + Retry-After
            4. primary (:9201) ── 429 / 3000ms timeout / down / 5xx ──▶ secondary (:9202)
            5. settle usage (or refund) ─▶ 200 + X-Gateway-Provider, X-RateLimit-*
```

## Quick start

```bash
cd task4-llm-router
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest -v
```

Run the full system in 3 terminals, each starting with `cd task4-llm-router && source .venv/bin/activate`:

```bash
MOCK_PROVIDER_NAME=primary   MOCK_PROVIDER_PORT=9201 python -m mock_providers   # terminal 1
MOCK_PROVIDER_NAME=secondary MOCK_PROVIDER_PORT=9202 python -m mock_providers   # terminal 2
python -m llm_router                                                            # terminal 3 (creates router.db)
```

## Demo (from a 4th terminal)

```bash
ask() { curl -s -i localhost:8200/v1/chat/completions -H "Authorization: Bearer ${1:-tenant-acme-key}" \
  -d '{"messages":[{"role":"user","content":"Summarize the quarterly report"}],"max_tokens":200}'; echo; }
mode() { curl -s localhost:$1/admin/mode -d "{\"mode\":\"$2\"}"; echo; }

ask                              # 200, X-Gateway-Provider: primary
mode 9201 rate_limited; ask      # 200, provider: secondary, X-Gateway-Fallback-Reason: rate_limited
mode 9201 slow;         ask      # ~3.0 s, then secondary, reason: timeout
mode 9201 error;        ask      # secondary, reason: server_error (primary's stack trace never shown)
mode 9202 error;        ask      # 502 {"error":{"code":"upstream_unavailable",...}}
mode 9201 ok; mode 9202 ok
```

**Rate limiting.** The `tiny` tenant has 2,000 tokens per minute. Five parallel requests that each reserve about 605 tokens, held in flight by the slow primary, leave 3 accepted and 2 rejected:

```bash
mode 9201 slow
for i in 1 2 3 4 5; do curl -s -w " [%{http_code}]\n" localhost:8200/v1/chat/completions \
  -H "Authorization: Bearer tenant-tiny-key" \
  -d '{"messages":[{"role":"user","content":"hi"}],"max_tokens":600}' | cut -c1-110 & done; sleep 5
```

Tenants, keys and limits are mock data in [`src/llm_router/data/tenants.json`](src/llm_router/data/tenants.json):

| API key             | Tenant | Tokens / minute |
| ------------------- | ------ | --------------- |
| `tenant-acme-key`   | acme   | 50,000          |
| `tenant-globex-key` | globex | 50,000          |
| `tenant-tiny-key`   | tiny   | 2,000           |

The mock providers' modes (`ok`, `rate_limited`, `slow`, `error`) can be switched at runtime with `POST /admin/mode`. Replies come from [`src/mock_providers/data/replies.json`](src/mock_providers/data/replies.json).

## Configuration

| Variable | Default |
| -------- | ------- |
| `ROUTER_PRIMARY_URL` / `ROUTER_PRIMARY_MODEL` | `http://127.0.0.1:9201/v1/chat/completions` / `primary-model` |
| `ROUTER_PRIMARY_TIMEOUT` | `3.0` seconds (whole-call deadline) |
| `ROUTER_SECONDARY_URL` / `ROUTER_SECONDARY_MODEL` | `http://127.0.0.1:9202/v1/chat/completions` / `secondary-model` |
| `ROUTER_SECONDARY_TIMEOUT` | `10.0` seconds |
| `ROUTER_DB_PATH` | `router.db` (git-ignored) |
| `ROUTER_WINDOW_SECONDS` | `60` |
| `ROUTER_DEFAULT_MAX_TOKENS` | `512` (reserved when a request sets no `max_tokens`) |
| `ROUTER_HOST` / `ROUTER_PORT` | `127.0.0.1` / `8200` |

## Project layout

```
src/llm_router/
├── app.py           request pipeline, response headers, standardized error handlers
├── rate_limiter.py  sliding-window token limiter on SQLite (acquire / adjust / release)
├── router.py        primary → secondary failover with a hard per-call deadline
├── tokens.py        request validation + token estimation
├── tenants.py       API key → tenant (data/tenants.json)
├── errors.py        error catalog + the one payload format
└── config.py
src/mock_providers/  switchable mock provider (ok / rate_limited / slow / error)
tests/               limiter, router and end-to-end tests
```

## Design

### Token-aware sliding window, stored in SQLite

- **Data model:** each admitted request is a row `(tenant_id, tokens, created_at)`. A tenant's usage is the sum of its rows from the last 60 seconds.
- **No boundary bursts:** unlike a fixed window, a token counts for exactly 60 seconds after it was reserved. There's no reset at the top of each minute that would allow double the budget in a burst.
- **Reserve, then settle:** the limiter has to decide before the model runs, so it reserves an **estimate**: prompt characters ÷ 4, plus per-message overhead, plus `max_tokens`. After the call, the row is updated to the provider's `usage.total_tokens`.
  - If the request fails, or the client disconnects, the reservation is **refunded**.
  - Usage is therefore accurate, and in-flight requests still count against the budget.
- **Eviction:** every acquire deletes all expired rows for *every* tenant, using an index on `created_at`. The table only ever holds the current minute, and a test checks that expired rows are physically removed.
- **Retry-After:** walks the tenant's rows from oldest to newest and returns the moment when enough tokens will have expired for this request to fit. It's capped by the window.
- **Requests larger than the budget** are rejected up front with `request_too_large`, because waiting would never help.

### Concurrency
- **Atomic check-and-reserve:** each decision runs in one `BEGIN IMMEDIATE` transaction, which takes SQLite's write lock up front. The steps are evict → sum → reserve-or-reject, so no other request can slip in between the check and the insert.
  - This holds across asyncio tasks: each limiter call runs in a worker thread with its own connection.
  - It also holds across **multiple gateway processes** sharing the file.
  - WAL mode keeps readers from blocking the writer.
- **Clock read inside the lock:** the clock is read only *after* the lock is acquired, so timestamps follow commit order.
  - This bug showed up during live testing as a `Retry-After: 61s` on a 60-second window.
  - A contention test guards against it.
- **What the tests show:**
  - 60 concurrent requests for 2,000 tokens against a 50,000 limit: exactly 25 accepted.
  - The same holds with two limiter instances sharing the file.
  - With transactions removed, 37 got through, and the test fails.

### Failover and the timeout race
- **One deadline for the whole call:** `asyncio.timeout(3.0)` covers connect, send and the full response read. A provider that trickles bytes can't stretch the call, which per-socket-read timeouts would allow.
- **When the deadline fires, the primary call is cancelled** and its connection closed. A late primary response therefore can't race the secondary's answer, and a test asserts the primary task really was cancelled.
- **Exact timing:** a primary that answers just *before* the deadline is used. One that misses it fails over at the deadline, observed at 3.015 s in a live run.
- **Failover triggers:** 429, timeout, connection failure, 5xx.
- **No failover on other 4xx:** the request itself is invalid, and the backup would reject it too.
- **Each provider gets its own model name,** e.g. primary `primary-model`, secondary `secondary-model`.
- **Response headers:** `X-Gateway-Provider` and `X-Gateway-Fallback-Reason` show which provider answered and why.

### Standardized, sanitized errors

```json
{"error": {"code": "rate_limit_exceeded", "message": "Token rate limit of 2000 tokens per minute exceeded. Retry after 42s.", "request_id": "req_3f9c0e1a7b2d4c5e"}}
```

| Situation | HTTP | `code` |
| --------- | ---- | ------ |
| Missing / unknown API key | 401 | `invalid_api_key` |
| Bad JSON, missing `messages`, `stream: true`, … | 400 | `invalid_request` |
| Request bigger than the whole budget | 400 | `request_too_large` |
| Budget exhausted | 429 | `rate_limit_exceeded` (+ `Retry-After`, `X-RateLimit-*`) |
| Both providers failed | 502 | `upstream_unavailable` |
| Both providers timed out (or one timed out and the other failed) | 504 | `upstream_timeout` |
| Unknown route / wrong method | 404 / 405 | `not_found` / `method_not_allowed` |
| Any unexpected exception | 500 | `internal_error` |

- **Gateway-written messages only.** Every message is written by the gateway, never copied from a provider. Upstream bodies are not even read into errors: the router logs only their size.
- **Catch-all handlers:** FastAPI's default error formats (`{"detail": ...}` and the HTML 500 page) are replaced, so even a 404 or a crash uses the standard shape.
- **Leak tests:** the tests inject a provider stack trace containing an internal IP and a secret key, plus an exception message containing a password. They then assert none of it reaches the client.
- **Request IDs:** every response carries `X-Request-Id`, which matches `request_id` in errors and in the logs, so support can trace a failure without exposing details.

## Production notes

- Replace the character-based token estimate with the model's real tokenizer, e.g. tiktoken. Only `tokens.py` would change.
- For many gateway hosts, move the limiter to Redis (a sorted set per tenant plus a Lua script), keeping the same `try_acquire / adjust / release` interface.
- Add a circuit breaker so a primary that keeps failing is skipped for a while instead of costing 3 s per request.
- Consider *hedged* requests: start the secondary after a shorter delay and take whichever answers first, cancelling the other.
- Support streaming (`stream: true`). Settle usage when the stream ends, and fail over only before the first token has been sent.
