# ShareLLM Gateway 🛡️⚡

**A resilient, OpenAI-compatible reverse proxy that keeps coding agents working when their
upstream model provider does not.**

Point Cursor, Cline, Aider — or any tool that accepts a custom OpenAI base URL — at this gateway
instead of a single provider, and stop losing work to `429 Too Many Requests`, `502 Bad Gateway`,
and upstream timeouts. The gateway fails over to your backup provider mid-stream, transparently:
the client just sees the answer continue.

![tests](https://img.shields.io/badge/tests-81%20passing-brightgreen)
![typing](https://img.shields.io/badge/typing-mypy%20--strict-blue)
![python](https://img.shields.io/badge/python-3.11%2B-informational)
![license](https://img.shields.io/badge/license-MIT-lightgrey)

> Badges are a snapshot of the state at the current release, not a live CI pipeline.

---

## Why

Coding agents are chatty: long prompts, tool calls, and streams that run for minutes. That makes them
unusually sensitive to provider flakiness — and the failure mode is nasty, because by the time a
provider returns an error, the agent has usually already committed to the stream.

ShareLLM Gateway sits between the agent and your providers and fixes exactly that:

- **It fails over before the client knows anything went wrong.** A first-chunk (TTFT) inspection
  checks the upstream status *before* the client's stream commits, so an error never becomes a
  truncated SSE stream on the agent's side.
- **It keeps your providers honest about model names.** The client asks for an alias
  (`gpt-6.1-sol`); each configured target receives the model id it actually expects
  (`gpt-6-astra` on the backup), so cross-provider failover works instead of producing a 404.
- **It cools down providers that just failed**, so the next request skips straight to a healthy
  target instead of paying the round-trip again.

## Features

| | |
|---|---|
| **Transparent SSE streaming** | Byte-exact `text/event-stream` pass-through, zero-copy — one chunk is read ahead, never the whole body |
| **Automatic failover** | On `429` / `500` / `502` / `503` / `504`, and on `httpx.TimeoutException` / `httpx.NetworkError` |
| **TTFT first-chunk inspection** | Upstream status is checked and the error body consumed *before* the client stream commits |
| **Per-target model rewriting** | Client alias → each target's own upstream model id, built on a per-attempt copy |
| **Soft cooldown circuit breaker** | A failed target is skipped for 30 s (configurable) — unless every target is cooling |
| **Anti-422 tool sanitization** | Strips `tools: []`, `tool_choice: "none"` (when it is a no-op) and non-object `stream_options` that strict upstreams reject |
| **ShareLLM error normalization** | `type: "share_llm_error"` → standard OpenAI `rate_limit_error` / `invalid_request_error` / `api_error` |
| **Prompt-cache telemetry** | Extracts `usage.prompt_tokens_details.cached_tokens` and logs the hit ratio |
| **OpenAI-compatible surface** | `POST /v1/chat/completions`, `GET /v1/models`, `GET /health` |
| **Header sanitation** | RFC 9110 §7.6.1 hop-by-hop headers (including `Connection`-nominated fields) are stripped; provider credentials replace inbound ones |
| **Strictly typed** | `mypy --strict` clean across the whole package |

## Quickstart

```bash
# 1. Install (editable, so your changes are picked up)
python -m pip install -e .

# 2. Configure — copy the example and add real provider keys
cp config.example.yaml config.yaml

# 3. Run
python -m gateway
```

```
============================================================
 ShareLLM Smart Gateway 0.1.0
============================================================
 Gateway URL  : http://127.0.0.1:8000/v1
 Health check : http://127.0.0.1:8000/health

 Active routes (2):
   - coding_route  (strategy: failover)
       models : gpt-6.1-sol, gpt-6-astra
       1. sharellm_primary  ->  gpt-6.1-sol  (timeout 15s)
       2. openrouter_backup  ->  gpt-6-astra  (timeout 30s)
   - chat_route  (strategy: failover)
       models : gpt-6.1-sol
       1. sharellm_primary  ->  gpt-6.1-sol  (timeout 10s)
       2. openrouter_backup  ->  gpt-6-astra  (timeout 20s)

 Cursor / Cline setup:
   Set Base URL to http://127.0.0.1:8000/v1
============================================================
```

Then in your IDE, set the OpenAI Base URL to `http://localhost:8000/v1` and use a model alias
from your route config (`gpt-6.1-sol` in the example). `python -m gateway --config other.yaml`
selects a different config file; without a flag it prefers `config.yaml` and falls back to
`config.example.yaml`.

> `config.yaml` is gitignored because it holds real keys. `config.example.yaml` is the template.

## Configuration

```yaml
gateway:
  host: 127.0.0.1
  port: 8000
  log_level: info

providers:
  sharellm_primary:
    type: sharellm            # "sharellm" | "openai"
    base_url: https://api.sharellm.example.com/v1
    api_key: sk-sharellm-REPLACE_WITH_REAL_KEY

  openrouter_backup:
    type: openai
    base_url: https://openrouter.ai/api/v1
    api_key: sk-or-REPLACE_WITH_REAL_KEY

routes:
  coding_route:
    models: ["gpt-6.1-sol", "gpt-6-astra"]   # aliases this route serves
    strategy: failover
    targets:                                  # tried in order
      - provider: sharellm_primary            # key into providers
        model: "gpt-6.1-sol"                  # the model id THIS target expects
        timeout_seconds: 15.0
      - provider: openrouter_backup
        model: "gpt-6-astra"
        timeout_seconds: 30.0
```

Every `target.provider` must exist in `providers`; a dangling reference fails fast at load time with
a descriptive error.

## How it works

```
 Cursor / Cline / Aider
        │  POST /v1/chat/completions   (model: "gpt-6.1-sol")
        ▼
 ┌─────────────────────────────────────────────┐
 │  ShareLLM Gateway                           │
 │  1. sanitize payload (anti-422)             │
 │  2. rewrite model per target                │
 │  3. strip hop-by-hop headers, set auth      │
 │  4. forward → inspect first chunk (TTFT)    │
 └─────────────────────────────────────────────┘
        │                        │
        ▼ 429 / 502 / timeout    ▼ 200
   sharellm_primary        openrouter_backup
   (cooldown 30 s)         model: "gpt-6-astra"  ──▶ 200 SSE, relayed byte-exact
```

## Testing & quality

```bash
python -m pytest -v                  # 81 tests
python -m mypy --strict src/gateway  # clean
```

| Suite | What it covers | Tests |
|---|---|---|
| `test_config.py` | Typed config loading, provider-reference validation, defaults | 3 |
| `test_proxy.py` | Header sanitation, transparent forwarding, SSE relay, abort cleanup | 9 |
| `test_failover.py` | Failover on status/timeout, cooldown, disconnect safety | 13 |
| `test_adapters.py` | Model rewriting, error normalization, cache telemetry, sanitizer | 36 |
| `test_e2e_chaos.py` | Full agent journey, CLI, zero-leak disconnect harness | 20 |

The suites are in-process and bind **no network ports** (`httpx.ASGITransport` + `respx`), and the
zero-leak guarantee is asserted directly: a client disconnect mid-stream releases the upstream
response at disconnect time, with exactly one close per stream.

## Limitations

Honest list, so you know what you are deploying:

- `route.strategy` is carried in the config but only `failover` is implemented — targets are always
  tried in configured order.
- The cooldown state is in-memory and per-process; it does not survive a restart or share across
  workers.
- Prompt-cache telemetry is emitted for non-streaming responses only. Streaming is zero-copy, so the
  gateway does not parse SSE bodies.
- No request auth, rate limiting, metrics or tracing yet.
- The `uvicorn.run` serve path is not exercised by the test suite (tests drive the ASGI app in-process).

## Roadmap

Round-robin and weighted routing strategies · persisted/shared cooldown state · streaming cache
telemetry · request auth and rate limiting · Prometheus / OpenTelemetry metrics.

## License

[MIT](./LICENSE) © 2026 ShareLLM Gateway Contributors
