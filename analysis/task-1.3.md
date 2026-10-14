# Task 1.3 — First-Chunk Inspection & Resilient Failover Engine

Project: **ShareLLM Smart Gateway** (`sharellm-gateway`)
Project root: `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway`

Task 1.2 (Core Reverse Proxy) is verified and merged. This task resolves the streaming
status/headers limitation identified during that review.

## Objective

Implement the **Resilient Failover Engine & First-Chunk Inspection**: when an upstream target
returns a retryable error (429, 500, 502, 503, 504) or hits a network/timeout error, the gateway
must seamlessly fall back to subsequent targets in the route without terminating the client
connection.

## Scope of work

### 1. `src/gateway/engine/router.py` (new)

Target selection and failover dispatching:

- Given a `RouteConfig` and `AppConfig`, iterate `route.targets` in configured priority order.
- Retryable HTTP statuses: `[429, 500, 502, 503, 504]`.
- Retryable exceptions: `httpx.TimeoutException`, `httpx.NetworkError`.
- **In-memory Soft Cooldown (circuit breaker):** if a target fails with 429 or 5xx, record a
  cooldown timestamp (e.g. 30 s). Future requests temporarily skip targets currently on cooldown,
  **unless all targets in the route are on cooldown**, in which case all are used again in order.

### 2. Refactor `src/gateway/engine/proxy.py`

- Restructure `forward_request` so streaming responses **carry the upstream status code and
  headers before the client stream commits**.
- **First-Chunk Buffer (TTFT inspection)** for streaming:
  - On upstream stream open, inspect `response.status_code`.
  - If `status_code >= 400`: consume the error body and return
    `(status_code, error_json, headers, None)` so the failover router can catch it and try the
    next target.
  - If `status_code == 200`: read the first data chunk from the upstream iterator to verify stream
    viability; construct a chained async generator that yields the buffered first chunk followed by
    the remaining `response.aiter_bytes()`; return `(200, None, headers, chunk_generator)`.
  - `_ReleasingStreamingResponse` must properly wrap the chained generator to preserve the
    zero-leak guarantee on client disconnect.

### 3. Update `src/gateway/main.py`

In `/v1/chat/completions`:

- Route requests through the failover engine instead of hardcoding `route.targets[0]`.
- On a retryable status or timeout, log a warning and immediately attempt the next target.
- On success: return a JSON response (buffered) or `_ReleasingStreamingResponse` carrying the
  **actual upstream status code and relayed headers** (streaming).
- If ALL targets fail: return the **final** target's error status code (e.g. 429 or 502) and an
  OpenAI-formatted envelope
  `{"error": {"message": "All upstream targets failed", "type": "gateway_failover_exhausted"}}`.

### 4. `tests/test_failover.py` (new)

Use `respx` and `httpx.ASGITransport`; mock upstream failures deterministically.

1. Buffered — target 1 returns 429 → fails over to target 2 → 200 OK.
2. Streaming — target 1 returns 502 → fails over to target 2 → 200 `text/event-stream` with exact
   SSE chunks.
3. Upstream timeout — target 1 raises `httpx.ReadTimeout` → fails over to target 2 → 200 OK.
4. All targets fail with 429 → 429 with the `gateway_failover_exhausted` envelope.
5. Cooldown — target 1 hits 429 → the next immediate request skips target 1 and routes directly to
   target 2.
6. Client disconnect during failover iteration releases all open client sessions without socket
   leaks.

## Constraints & coding standards

- **Maintain full backward compatibility with the tests from 1.1 and 1.2.**
- **Zero socket leaks on disconnect.**
- Deterministic, zero-network-bound unit test suite.
- Fully typed (`mypy` compliant).

## Resolution of the contract conflict (explicit instruction)

The spec above mandates changing `forward_request`'s streaming return from
`AsyncIterator[bytes]` to the 4-tuple `(status_code, error_json, headers, chunk_generator|None)`.
That legitimately invalidates the two **engine-level** call sites in `tests/test_proxy.py`
(currently around lines 272 and 332), which do `iterator = await forward_request(...)` followed by
`iterator.__anext__()`.

These call sites must be updated **minimally** to unpack the new 4-tuple, with every existing
assertion's intent preserved (per-chunk relay, no buffering, in-order delivery, abort cleanup).
This is mechanical adaptation to a spec-mandated contract change — not a licence to weaken tests.

- `tests/test_config.py` (Task 1.1) must **not** be touched at all.
- The other `tests/test_proxy.py` tests must pass unmodified.
- Any change to `tests/test_proxy.py` must be listed explicitly in the handoff with a before/after
  of each touched assertion, so the Reviewer can adjudicate it.

## Definition of done

- `src/gateway/engine/router.py` and `tests/test_failover.py` exist with complete implementations.
- `pytest` passes from the project root: the 3 Task 1.1 config tests, the 9 Task 1.2 proxy tests
  (with the two engine-level streaming call sites adapted), and the 6 new failover tests.
- No real network ports are bound.
