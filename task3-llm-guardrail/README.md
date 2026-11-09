# Task 3: LLM Gateway Streaming Guardrail (PII Redaction)

An OpenAI-compatible LLM gateway that proxies `POST /v1/chat/completions` to a model provider. It redacts emails, SSNs and credit card numbers from the response **while it streams**, replacing each with `[REDACTED]`. It never buffers the full response.

```
client ──▶ gateway (:8100) ──▶ LLM provider (:9100, mock)
       ◀── redacted SSE ◀───── SSE chunks: "Mail jo" "hn@exam" "ple.com" ...
                │
                └─ per chunk: emit what is provably safe, hold back only a possible-PII tail
```

## Quick start

```bash
cd task3-llm-guardrail
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest -v                      # run the test suite

python -m mock_llm_provider    # terminal 1: mock provider on :9100
python -m llm_gateway          # terminal 2: gateway on :8100
```

Stream through the gateway. `-N` disables curl's buffering so you can see the chunks arrive:

```bash
curl -N localhost:8100/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model":"mock-gpt","stream":true,"messages":[{"role":"user","content":"contact details please"}]}'
```

The mock provider picks its reply by keyword (`contact`, `payment`, `identity`, or a default) from [`data/responses.json`](src/mock_llm_provider/data/responses.json). It streams the reply in 2–7 character chunks, so PII is split across chunk boundaries.

To use a real provider instead, point the gateway at any OpenAI-compatible endpoint:

```bash
LLM_UPSTREAM_URL=https://api.openai.com/v1/chat/completions LLM_UPSTREAM_API_KEY=sk-... python -m llm_gateway
```

## Configuration

| Variable                  | Default                                         |
| ------------------------- | ----------------------------------------------- |
| `LLM_UPSTREAM_URL`        | `http://127.0.0.1:9100/v1/chat/completions`     |
| `LLM_UPSTREAM_API_KEY`    | unset; when set, sent to the provider as a Bearer token |
| `LLM_UPSTREAM_TIMEOUT`    | `30` (seconds)                                  |
| `LLM_GATEWAY_HOST` / `LLM_GATEWAY_PORT` | `127.0.0.1` / `8100`              |
| `MOCK_LLM_PORT` / `MOCK_LLM_CHUNK_DELAY_MS` | `9100` / `40`                 |

## Project layout

```
src/llm_gateway/
├── app.py        endpoint: forward request, choose streaming / non-streaming, sanitize errors
├── guardrail.py  SSE chunk transformer: one StreamingRedactor per choice index
├── redactor.py   StreamingRedactor: incremental redaction with a bounded hold-back buffer
├── patterns.py   PII regexes (bounded) + Luhn check + full-text redaction
├── sse.py        line-by-line SSE parser / formatter
└── config.py
src/mock_llm_provider/   OpenAI-style streaming mock + data/responses.json
tests/
├── test_redactor.py     detection rules, chunking property tests, latency, memory bound
├── test_guardrail.py    time-to-first-token, per-choice state, edge events
└── test_gateway.py      end-to-end incl. provider errors and mid-stream failure
```

## How the streaming redactor works

**The problem:** PII can be split across chunks, e.g. `"Mail jo"` → `"hn@exam"` → `"ple.com"`.
- If each chunk is redacted on its own, `jo` and `hn@exam` reach the client before the email is recognised.
- If the whole response is buffered first, there is no streaming, and both TTFT and memory get worse.

**The approach:** after each chunk, emit everything that is provably final, and **hold back only the tail that could still become PII**.

1. Run the PII regex over *held tail + new chunk* using the [`regex`](https://pypi.org/project/regex/) library's `partial=True` mode. A *partial match* is one that ran into the end of the buffer and could continue with more input, for example `john@ex`. Text is held back from the start of that match.
2. A *complete* match can also still grow. `a@b.co.` can become `a@b.co.uk`, and `123-45-6789` can become `123-45-67890`, which is no longer an SSN. `fullmatch(text, pos=match.start(), partial=True)` checks whether the rest of the buffer is still a valid prefix of a longer match. If it is, the match is held too.
3. Everything before the hold point is redacted and emitted immediately.
4. When the stream ends (`finish_reason` or `[DONE]`), the held tail is flushed with final redaction.

**Correctness guarantee:** streaming output is identical to redacting the complete text in one go, for *any* chunking. The tests check this for every two-way split point, every single-character chunking and 300 random chunkings of each sample. A naive redact-each-chunk implementation fails 8 tests.

### Latency (TTFT)
- Non-content chunks are forwarded immediately, e.g. the initial `role: assistant` chunk.
- Ordinary text is released as soon as it arrives, except for the **last partial word** of each chunk. Any word could still turn into an email local part, so it waits for the next chunk. That's roughly one token of extra latency, and only when needed.
- Chunks whose only text is held back aren't sent at all, so the client never receives empty deltas.

### Memory
- Every pattern uses **bounded repetition**, e.g. `{1,64}` for an email local part and 13–19 digits for a card. So no single match can exceed a few hundred characters, and the held-back buffer is bounded by that length, not by the response length.
  The tests stream 100,000 characters of PII-like noise and assert that the buffer stays under 400 characters (it peaked at 76 in development).
- Text that has been emitted is never scanned again. Each chunk costs O(held tail + chunk length).
- SSE is parsed line by line, so at most one event is held at a time.

### Detection rules

| Type   | Pattern                                    | Notes |
| ------ | ------------------------------------------ | ----- |
| Email  | `local{1,64}@domain{1,253}.tld{2,24}`      | |
| SSN    | `\bddd-dd-dddd\b`                           | `123-45-67890` and `A123-45-6789` are not matched |
| Card   | 13–19 digits, optional space/dash separators | Must pass the **Luhn** checksum, so order IDs and timestamps aren't redacted |

Word boundaries (`\b`) are evaluated with one character of lookbehind context carried across chunks. They behave exactly as they would on the full text.

## Error handling

| Situation | Client receives |
| --------- | --------------- |
| Body is not JSON | `400`, `invalid_request` |
| Provider returns 429 | `429`, `rate_limited` |
| Provider returns 400/422 | `400`, `invalid_request` |
| Provider returns another error | `502`, `upstream_error` |
| Provider unreachable / timeout | `502`, `upstream_unavailable` / `504`, `upstream_timeout` |
| Provider drops mid-stream | an SSE `error` event, then the stream ends. **Held-back text is discarded**, so a half-received email is never flushed |

Error bodies never include the provider's response body or stack traces. The provider API key is added by the gateway; clients never send or see it.

## Production notes

- Extend the patterns, e.g. phone numbers, IBANs, API keys, or plug in an NER model for names and addresses. Only `patterns.py` would change.
- Redact `tool_calls[].function.arguments` deltas as well, since models can put PII into tool arguments.
- Add gateway authentication and rate limiting (see Tasks 2 and 4).
- Emit metrics: redactions per type, held-back buffer size, added latency per chunk.
