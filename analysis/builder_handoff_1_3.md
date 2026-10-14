# Builder Handoff — Task 1.3

STATUS: CHANGES_APPLIED (round 1 — see `## FIXES APPLIED (round 1)` at the bottom; the round-0
handoff is preserved verbatim above it)

Task: **First-Chunk Inspection & Resilient Failover Engine** for the ShareLLM Smart Gateway
(`C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway`).

Implemented the failover engine (`src/gateway/engine/router.py`), refactored the proxy engine
(`src/gateway/engine/proxy.py`) so streaming carries status + headers and performs TTFT
inspection, wired `/v1/chat/completions` through the engine (`src/gateway/main.py`), and added
`tests/test_failover.py`.

Full suite from the project root: **`25 passed in 3.32s`** (3 config + 9 proxy + 13 failover).
Task 1.1's `tests/test_config.py` is byte-identical (SHA-256 recorded below).

> ⚠️ **One deviation from the task's stated premise, flagged up front.** The task said only the
> two engine-level streaming call sites in `tests/test_proxy.py` (~lines 272 and 332) would
> break. A **third** test broke: `test_non_json_upstream_error_body_is_wrapped` mocked a **502**,
> and 502 is now a *retryable* status, so the failover engine (correctly) fails over to the
> unmocked backup target. That test is adapted too (hunk 3 below). See *Known limitations* #1.

---

## Files created / changed (absolute paths)

| Path | Status | Purpose |
| --- | --- | --- |
| `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway/src/gateway/engine/router.py` | **created** | Failover engine: `dispatch()`, `CooldownRegistry`, retryable status/exception policy, all-fail envelope. |
| `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway/src/gateway/engine/proxy.py` | **modified** | `forward_request` streaming return is now the 4-tuple `(status, error_body, headers, chunk_iterator\|None)`; TTFT inspection; chained generator; streaming relays real upstream status + headers. |
| `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway/src/gateway/main.py` | **modified** | `/v1/chat/completions` routes through `dispatch()`; creates the `CooldownRegistry` in the lifespan on `app.state`; streaming success returns the real status/headers. |
| `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway/tests/test_failover.py` | **created** | The 6 required failover tests + the non-retryable pass-through test + cooldown/registry unit tests (13 total). |
| `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway/tests/test_proxy.py` | **modified (3 hunks)** | Mechanical adaptation to the spec-mandated contract change (hunks 1–2) + one further required adaptation (hunk 3). Full diff below. |

Untouched: `tests/test_config.py`, `src/gateway/config.py`, `src/gateway/__init__.py`,
`config.example.yaml`, `pyproject.toml`.

---

## The exact `tests/test_proxy.py` diff

Three hunks. Nothing else in the file changed.

### Hunk 1 — engine-level streaming call site (was ~line 272), in `test_streaming_chat_completion_forwards_sse_chunks` part (A)

**BEFORE**
```python
        iterator = await forward_request(
            gateway.app.state.http_client,
            PRIMARY_TARGET,
            PRIMARY_PROVIDER,
            inbound_payload,
            True,
            {},
        )

        first_chunk = await iterator.__anext__()
```
**AFTER**
```python
        status_code, error_body, response_headers, iterator = await forward_request(
            gateway.app.state.http_client,
            PRIMARY_TARGET,
            PRIMARY_PROVIDER,
            inbound_payload,
            True,
            {},
        )
        assert status_code == 200
        assert error_body is None
        assert iterator is not None

        first_chunk = await iterator.__anext__()
```
**Justification.** Mechanical unpack of the new 4-tuple. The three added assertions *strengthen*
the test: they pin the success shape (`200`, no error body, a real iterator) that the old
single-value contract could not express. **Every original assertion is untouched**:
`first_chunk == SSE_CHUNKS[0]`, `engine_stream.released == [SSE_CHUNKS[0]]` (no buffering, still
exactly one chunk read ahead), `[first_chunk, *remaining] == SSE_CHUNKS` (in-order), and
`engine_stream.closed is True` after a full drain.

### Hunk 2 — engine-level streaming call site (was ~line 332), part (C)

**BEFORE**
```python
        iterator = await forward_request(
            gateway.app.state.http_client,
            PRIMARY_TARGET,
            PRIMARY_PROVIDER,
            inbound_payload,
            True,
            {},
        )
        assert await iterator.__anext__() == SSE_CHUNKS[0]
```
**AFTER**
```python
        _status_code, _error_body, _response_headers, iterator = await forward_request(
            gateway.app.state.http_client,
            PRIMARY_TARGET,
            PRIMARY_PROVIDER,
            inbound_payload,
            True,
            {},
        )
        assert iterator is not None
        assert await iterator.__anext__() == SSE_CHUNKS[0]
```
**Justification.** Mechanical unpack. The unpacked-but-unused elements are underscore-prefixed
because this part only exercises the abort path. The one added `assert iterator is not None`
narrows the `Optional` iterator (required by the new contract) before `__anext__()`. **The
original abort/`aclose` assertions are untouched**: `abandoned_stream.closed is False` before
`aclose()` and `abandoned_stream.closed is True` after.

### Hunk 3 — `test_non_json_upstream_error_body_is_wrapped` (was line 446; **not anticipated by the task**)

**BEFORE**
```python
    """A non-JSON (HTML) upstream error body yields a 502 envelope, not a crash."""
    with respx.mock(assert_all_called=False) as mock:
        mock.post(UPSTREAM_URL).mock(
            return_value=httpx.Response(
                502,
                content=b"<html>Bad Gateway</html>",
                headers={"content-type": "text/html"},
            )
        )
        ...
    assert response.status_code == 502
    ...
    assert "HTTP 502" in body["error"]["message"]
    assert "<html>Bad Gateway</html>" in body["error"]["message"]
```
**AFTER**
```python
    """A non-JSON (HTML) upstream error body yields an envelope, not a crash.

    The mocked upstream status is deliberately a **non-retryable** one (400).
    With Task 1.3's failover engine, the retryable set (429/500/502/503/504)
    fails over to the next target before the body ever reaches the caller, so a
    502 here would be swallowed by failover rather than relayed. A non-retryable
    status is relayed straight back with the upstream body re-serialised as JSON,
    which is exactly the path this test pins.
    """
    with respx.mock(assert_all_called=False) as mock:
        mock.post(UPSTREAM_URL).mock(
            return_value=httpx.Response(
                400,
                content=b"<html>Bad Request</html>",
                headers={"content-type": "text/html"},
            )
        )
        ...
    assert response.status_code == 400
    ...
    assert "HTTP 400" in body["error"]["message"]
    assert "<html>Bad Request</html>" in body["error"]["message"]
```
**Justification (why this was unavoidable).** The test mocked `502` on the *primary* target only,
with the backup target unmocked. `502` is in the spec's retryable set, so the engine now fails
over to `openrouter_backup` — which `respx` (default `assert_all_mocked=True`) rejects with
`AllMockedAssertionError`, producing a 500 instead of the asserted 502. There is **no**
implementation that both (a) fails over on 502 as the spec mandates and (b) lets this test pass
unmodified. The adaptation keeps the test's **subject and intent** — a non-JSON upstream body is
wrapped by `_non_json_body_error` into the `upstream_error` envelope rather than raising, with the
upstream status propagated and the body re-serialised as JSON — by choosing a status that is
*not* retryable. Every assertion shape is preserved; only the status number and the matching HTML
string changed. This is the smallest change that keeps the `upstream_error` envelope covered at
the app boundary. The 502-triggered-failover behaviour is covered instead by
`test_failover.py::test_streaming_failover_relays_backup_sse_byte_exact`.

---

## Key design decisions

### 1. Typing the dual-flow returns (`proxy.py`)

Two type aliases are exported so the router and endpoint share them:

```python
BufferedResult: TypeAlias = tuple[int, JSONValue, dict[str, str]]
StreamingResult: TypeAlias = tuple[int, JSONValue, dict[str, str], AsyncIterator[bytes] | None]
```

`forward_request` keeps its **exact** six-parameter signature with `stream: bool` and two
`@overload`s on `Literal[False]` → `BufferedResult` and `Literal[True]` → `StreamingResult`.
`dispatch` mirrors the same pattern (two overloads, `Literal[False]`/`Literal[True]`). `JSONValue`
(`dict[str, Any] | list[Any] | str | int | float | bool | None`) is used for both bodies so no
bare `Any` appears as a return type; `Any` only appears inside that recursive alias and in
`dict[str, Any]` request payloads, where heterogeneous JSON genuinely requires it. Verified with
`typing.get_overloads` → 2 for each function and `inspect.signature` (see V5). `mypy` is **not
installed** in the environment, so no mypy run was possible — typing is enforced by full
annotations, the overload pairs and Pydantic.

### 2. Cooldown registry — scoping and reset (`router.py`)

`CooldownRegistry` is an ordinary class; **no module-global state**. It is constructed in the ASGI
lifespan (`main.py`) and stored as `app.state.cooldown_registry`, so every app instance — and
therefore every test that starts a fresh app via the `gateway` fixture — starts from a clean
registry. It exposes `record_failure(target)`, `is_cooling(target)` and **`reset()`**. The TTL
(`DEFAULT_COOLDOWN_SECONDS = 30.0`) and the clock (`time.monotonic` by default) are constructor
parameters, so expiry is testable without sleeping
(`test_cooldown_registry_expires_and_resets` drives an injected clock to the exact TTL boundary).
Target identity is the `(provider, model)` pair. `_select_targets` returns the non-cooling
targets when at least one exists, otherwise the full ordered list.

> This scoping was validated the hard way: my first draft of `test_failover.py` looped 10× against
> one app and asserted the primary was attempted every time — it failed from iteration 2 onward
> because the registry (correctly) skipped the cooled-down primary. The test now calls
> `registry.reset()` at the top of each iteration, which both fixes the test and exercises the
> required reset API.

### 3. Chained generator and the zero-leak guarantee (`proxy.py`)

`_forward_streaming` opens the response with `await client.send(request, stream=True)` (never the
nested `client.stream(...)` context manager) so the close is a direct `await response.aclose()`
rather than an `athrow` through `__aexit__` — the Task 1.2 F1 fix is preserved.

* **TTFT success:** the first chunk is pulled eagerly with `await upstream.__anext__()` to prove
  stream viability *before* the caller's stream commits; only that one chunk is read ahead.
* **`_chained_stream(response, first_chunk, upstream)`** yields the buffered first chunk, then
  `async for chunk in upstream`, with `try/finally: await response.aclose()`. Because the
  `try` block wraps the **buffered first chunk's `yield`**, abandoning the stream while parked
  there still runs the `finally`. Combined with `_ReleasingStreamingResponse` (unchanged from
  1.2) owning the iterator's lifetime, the upstream is released *at disconnect time* on every
  exit path.
* **Failure before the first chunk** (e.g. a read timeout proving viability) closes the response
  in an `except BaseException: await response.aclose(); raise` — no half-open stream leaks.

### 4. Non-retryable statuses (`router.py`)

Only `RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}` and
`(httpx.TimeoutException, httpx.NetworkError)` cause a failover. Everything else returns
immediately: the loop does `return status_code, body, response_headers[, chunk_iterator]` for any
status not in the retryable set, so a `400`/`401`/`403`/`404`/`422` is relayed with the upstream
body and the remaining targets are never consulted. The `except` clause is a **specific two-class
tuple**, never a bare `Exception` (verified: no `except Exception` / bare `except` under
`src/gateway`). Covered by
`test_failover.py::test_non_retryable_status_is_relayed_without_failover` (asserts backup
`call_count == 0`).

### 5. All-targets-failed envelope (`router.py`)

The loop tracks `last_status_code` / `last_headers` from the final failed attempt. When the loop
exhausts, `_failover_exhausted_envelope()` returns a freshly-built
`{"error": {"message": "All upstream targets failed", "type": "gateway_failover_exhausted"}}` —
**not** `main._error_response`, which hard-codes `invalid_request_error`. The **final target's
status** is returned (e.g. 429 for all-429), never a hard-coded 502. A timeout maps to `504`, a
network error to `502` (neither produces an upstream status). For a streaming request the shape is
`(status, envelope, headers, None)`, so the endpoint returns a `JSONResponse` rather than
committing an SSE stream.

### 6. Streaming success — exactly what is relayed

`_forward_streaming` returns the upstream's real `status_code` and
`_forwardable_response_headers(response)` (the same filter the buffered path uses: it strips
hop-by-hop headers plus `content-length`/`content-encoding`/**`content-type`**). The endpoint then
builds:

```python
_ReleasingStreamingResponse(
    chunk_iterator,
    status_code=status_code,          # real upstream status, never a hard-coded 200
    headers=response_headers,         # relayed end-to-end headers (e.g. x-request-id)
    media_type="text/event-stream",   # supplies content-type, which the filter strips
)
```

So `x-request-id`, `x-ratelimit-*`, `retry-after`, etc. now reach the caller on `stream=True`
(**F6 fixed**), and a non-2xx streaming status is surfaced instead of 200 (**F2 fixed**). The
endpoint never hard-codes 200. Verified in V6.

---

## Verification evidence

Environment: `C:/Users/Notebook/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe`
(Python 3.13.14, pytest 9.1.1, pytest-asyncio 1.4.0 STRICT, respx 0.23.1, httpx 0.28.1,
fastapi 0.143.1, starlette 1.8.0). Nothing was installed. `mypy` is absent.

### V1 — full suite from the project root (the required command)

```
cd "C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway" && C:/Users/Notebook/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe -m pytest -v
```

```
collected 25 items

tests/test_config.py::test_example_config_loads_and_resolves_references PASSED [  4%]
tests/test_config.py::test_unknown_provider_reference_raises PASSED      [  8%]
tests/test_config.py::test_minimal_config_applies_documented_defaults PASSED [ 12%]
tests/test_failover.py::test_buffered_failover_on_retryable_status PASSED [ 16%]
tests/test_failover.py::test_streaming_failover_relays_backup_sse_byte_exact PASSED [ 20%]
tests/test_failover.py::test_timeout_fails_over_to_backup PASSED         [ 24%]
tests/test_failover.py::test_streaming_timeout_fails_over_to_backup PASSED [ 28%]
tests/test_failover.py::test_all_targets_retryable_returns_failover_exhausted_envelope PASSED [ 32%]
tests/test_failover.py::test_all_targets_retryable_streaming_returns_json_envelope PASSED [ 36%]
tests/test_failover.py::test_cooldown_skips_recently_failed_target PASSED [ 40%]
tests/test_failover.py::test_all_targets_cooling_falls_back_to_full_order PASSED [ 44%]
tests/test_failover.py::test_cooldown_registry_expires_and_resets PASSED [ 48%]
tests/test_failover.py::test_cooldown_registries_are_independent_instances PASSED [ 52%]
tests/test_failover.py::test_retryable_status_codes_match_the_spec PASSED [ 56%]
tests/test_failover.py::test_non_retryable_status_is_relayed_without_failover PASSED [ 60%]
tests/test_failover.py::test_client_disconnect_during_failover_releases_every_stream PASSED [ 64%]
tests/test_proxy.py::test_health_endpoint PASSED                         [ 68%]
tests/test_proxy.py::test_models_endpoint_aggregates_and_deduplicates PASSED [ 72%]
tests/test_proxy.py::test_non_streaming_chat_completion_is_transparent PASSED [ 76%]
tests/test_proxy.py::test_streaming_chat_completion_forwards_sse_chunks PASSED [ 80%]
tests/test_proxy.py::test_unmapped_model_returns_404 PASSED              [ 84%]
tests/test_proxy.py::test_request_header_sanitisation_is_case_insensitive_and_honours_connection PASSED [ 88%]
tests/test_proxy.py::test_forwardable_response_headers_drop_framing_headers PASSED [ 92%]
tests/test_proxy.py::test_non_json_upstream_error_body_is_wrapped PASSED [ 96%]
tests/test_proxy.py::test_client_disconnect_releases_upstream_stream_at_disconnect_time PASSED [100%]

============================= 25 passed in 3.32s ==============================
```

**Summary line: `25 passed in 3.32s`.**

### V2 — test isolation / no ordering dependence from cooldown state

```
=== test_failover.py ALONE ===     13 passed in 1.07s
=== full suite ===                 25 passed in 3.65s
=== failover then proxy ===        22 passed in 1.20s
=== proxy then failover ===        22 passed in 1.18s
=== config alone ===                3 passed in 2.05s
```
Same result in every order. (`CooldownRegistry` is per-app; `test_cooldown_registries_are_independent_instances`
asserts two registries never share state.)

### V3 — TTFT error path releases the upstream response (`status >= 400`, engine level)

```
PROBE1 status= 429 gen= None body= {'error': 'rate limited'}
PROBE1 upstream closed right after failover decision: True released: [b'{"error": "rate limited"}']
```
The response is `aread()`-ed and `aclose()`-d inside `_read_error_body` *before* the router makes
its failover decision — no leaked connection on the error path (trap #3).

### V4 — no leak through the chained generator (real ASGI disconnect, parked on the buffered first chunk)

20 runs through the real ASGI app on the `spec_version 2.3` path, with the client stalling after
the first body chunk so the generator is parked on the **buffered first chunk's `yield`** when the
disconnect lands. The closure is checked synchronously, with **no `gc.collect()`** in between:

```
DISCONNECT PROBE: closed at disconnect time (before gc): 20/20
DISCONNECT PROBE: closed after gc.collect():            20/20
DISCONNECT PROBE: loop-level exceptions: 0/20 []
DISCONNECT PROBE: only the buffered first chunk was read ahead on every run
```
`20/20` closed *before* any GC — release happens at disconnect time, not via the async-generator
finalizer (trap #4).

### V5 — signatures, overloads, import inertness

```
IMPORT: app type = FastAPI
IMPORT: has http_client before lifespan: False
IMPORT: has cooldown_registry before lifespan: False

SIGNATURE forward_request: (client, target, provider, request_data, stream: 'bool', headers) -> 'BufferedResult | StreamingResult'
overloads: 2
    stream: Literal[False] -> tuple[int, <JSONValue>, dict[str, str]]
    stream: Literal[True]  -> tuple[int, <JSONValue>, dict[str, str], Optional[AsyncIterator[bytes]]]
SIGNATURE dispatch: (client, route, config, request_data, stream: 'bool', headers, registry) -> 'BufferedResult | StreamingResult'
dispatch overloads: 2
router exports: ['CooldownRegistry', 'DEFAULT_COOLDOWN_SECONDS', 'RETRYABLE_STATUS_CODES', 'dispatch']
proxy exports: ['BufferedResult', 'JSONValue', 'StreamingResult', 'forward_request']
```
The six-parameter `forward_request` signature is unchanged; importing `gateway.main` binds no port,
spawns no task and reads no config.

### V6 — F2 and F6 resolved

```
F6 streaming status: 200 content-type: text/event-stream; charset=utf-8
F6 relayed x-request-id: req-stream-1 | x-ratelimit-remaining: 42
F6 body: b'data: {"c":1}\n\ndata: [DONE]\n\n'
F2 streaming non-retryable status: 400 | content-type: application/json | body: {'error': {'message': 'bad model'}}
```
**F6 (streaming dropped upstream headers): RESOLVED** — `x-request-id` / `x-ratelimit-remaining`
are relayed on `stream=True`. **F2 (streaming always returned 200): RESOLVED** — a streaming
request now surfaces the upstream's real status (400 here, 429/504 in the failover tests) instead
of 200.

### V7 — zero-copy / no buffering still holds (gated upstream)

```
PROBE2 first buffered chunk: b'data: 1\n\n' | upstream log: ['produced chunk-1'] | gate set: False
PROBE2 upstream closed before abandon: False
PROBE2 upstream closed at abandon (no gc): True | log: ['produced chunk-1', 'aclose() called']
PROBE3 chunk1 within 2s: b'data: 1\n\n' | upstream log: ['produced chunk-1'] | gate2 set: False
PROBE3 chunk2 after gate: b'data: 2\n\n'
PROBE3 log: ['produced chunk-1', 'produced chunk-2', 'aclose() called'] | closed: True
```
The first chunk is delivered while chunk 2 is still gated (a buffering implementation would have
hung and hit the 2 s `wait_for`). Exactly one chunk is read ahead.

### V8 — no real ports bound

Audit hook (`sys.addaudithook`) over the full suite:

```
25 passed in 2.62s
PYTEST_EXIT 0
bind hosts: ['127.0.0.1'] | bind ports: [0]
any non-zero (fixed) port bound: False
socket.getaddrinfo calls: 0
```
Every bind is `127.0.0.1:0` (the Windows event-loop self-pipe); no fixed port, no DNS.

### V9 — Task 1.1 artefacts byte-identical (SHA-256)

```
fd203d6c652d9c523c2f6840e7e45bd9e08cf347aad2e548ed5152b77c1a2f90    4859  tests/test_config.py
cee462a0a5b5fdc69d73b3457a3ff3b25a250f7020889a49a602e2709dcf5595    9250  src/gateway/config.py
bcad860f783d924d8c6403b5d03661c44000f928eb745f3fa2b2ada60a5c3fb4     526  src/gateway/__init__.py
65e12e3884a7bf25278f595278b6f15d9deb9bd244dfb1bb797a9909d46f81d7    1442  config.example.yaml
385809c9b63c99a79074a43639ad24685a29a94e5e60d5e3d584cf2e51ae9370     717  pyproject.toml
```
`tests/test_config.py` is unchanged (matches the 1.2 handoff's `fd203d6c652d9c52` / 4859 bytes),
as are `config.py`, `__init__.py`, `config.example.yaml` and `pyproject.toml`.

### V10 — hygiene

No `TODO`/`FIXME`/`XXX`/`HACK`/`placeholder`/`stub`/`mock` markers and no `print()` calls under
`src/gateway`; no bare `except Exception` / `except:`. Logging is via
`logging.getLogger(__name__)` and a warning is emitted on every failover (visible in the test
output: `WARNING gateway.engine.router: Upstream target 'sharellm_primary' (model 'gpt-6.1-sol')
failed (HTTP 429); failing over. 1 target(s) remaining.`).

---

## Known limitations / follow-ups

1. **`tests/test_proxy.py` needed a third adaptation beyond the two the task predicted.** The
   task asserted only the two engine-level call sites (~272, ~332) would break; the 502-based
   `test_non_json_upstream_error_body_is_wrapped` also broke, because 502 is a retryable status.
   It is adapted (hunk 3) with the intent preserved and no assertion weakened. **This is the one
   place where I could not satisfy "all other `tests/test_proxy.py` tests must pass unmodified"**;
   the spec's own retryable-status set makes it impossible. Reviewer adjudication requested.
2. **The `model` field is not rewritten per target — an *unmade design decision*, not a forced
   one (corrected; see `## FIXES APPLIED (round 1)` → (a)).** `forward_request` forwards the
   caller's JSON body verbatim, so the backup target receives the caller's alias (`gpt-6.1-sol`)
   rather than the target's `model` (`gpt-6-astra`). Forwarding verbatim was chosen deliberately
   to preserve transparency (the gateway relays the caller's body unchanged), and the 1.3 spec is
   silent on model translation. It is **not** forced by backward compatibility: rewriting `model`
   to `target.model` would be a no-op in `test_non_streaming_chat_completion_is_transparent` (its
   payload model `gpt-6.1-sol` already equals `coding_route.targets[0].model`), so
   `json.loads(upstream_request.content) == inbound_payload` would still pass. A multi-provider
   gateway arguably *should* rewrite `model`; left unchanged here because it is a behaviour change
   outside this task's spec. A future task may add an explicit model-rewrite step.
   **Follow-up candidate (escalated to the user by the orchestrator).**
3. **Streaming success forces `media_type="text/event-stream"`.** `_forwardable_response_headers`
   strips `content-type` (shared with the buffered path), so the endpoint supplies
   `text/event-stream`. A streaming upstream that returned a different content type would be
   relabelled. Correct for a chat-completions SSE proxy; noted for completeness.
4. **The all-fail envelope relays the final target's forwardable headers.** The body is
   gateway-generated, but headers such as `retry-after` / `x-ratelimit-*` are still useful to the
   caller. The spec is silent; `content-type` is stripped so framing stays consistent.
5. **`mypy` is not installed**, so no static-analysis run was performed. Typing is enforced by
   full annotations, the two `@overload` pairs and Pydantic; the public signatures were inspected
   at runtime (V5). If mypy is available in the review environment, `src/gateway` is the target.
6. **Cooldown is process-local and in-memory.** Restarting the gateway clears it, and it is not
   shared across workers. The spec asks for exactly this ("in-memory soft cooldown").
7. **`_ReleasingStreamingResponse` and the `client.send(..., stream=True)` + `try/finally`
   structure from Task 1.2 are unchanged** — this task builds on them rather than replacing them.
8. **Task 1.2's F2 and F6 are both resolved** (see V6). F2 (streaming always 200) is fixed by the
   new `(status, error_body, headers, chunk_iterator|None)` contract; F6 (streaming dropped
   upstream headers) is fixed by relaying `_forwardable_response_headers` on the
   `_ReleasingStreamingResponse`.

---

## EVIDENCE PACKAGE (minimal reviewer reading list)

1. `src/gateway/engine/router.py`
2. `src/gateway/engine/proxy.py`
3. `src/gateway/main.py`
4. `tests/test_failover.py`
5. `tests/test_proxy.py` (only hunks 1–3 above changed)
6. `config.example.yaml` (read-only fixture the tests derive URLs from)

---

## FIXES APPLIED (round 1)

Round-1 verdict: **APPROVED** (no BLOCKER, no MAJOR; every spec item MET, including an independent
40-run disconnect harness). Two items actioned this round:

* **(a)** a wording correction in this handoff (Reviewer/orchestrator refuted the "forced"
  justification for the verbatim `model` forwarding) — **documentation only, no code change**;
* **(b)** finding 2 (NIT) — the misleading failover log line on the last target — **code fix**.

No behaviour changed other than the log wording. `tests/test_config.py` is untouched; the
retryable sets, cooldown logic, TTFT path and response contracts are unchanged.

---

### (a) `target.model` — the "forced by backward compatibility" claim is withdrawn

**What the handoff originally claimed.** *Known limitations* #2 originally framed verbatim
forwarding as preserving Task 1.2's transparency contract, implying it was effectively forced.
That was wrong, and it has been rewritten in place (see the corrected *Known limitations* #2
above).

**Accurate statement.** Forwarding the caller's JSON body verbatim is an **unmade design
decision**, not a forced one. Rewriting the outgoing `model` to `RouteTargetConfig.model` would be
a **no-op** for `test_non_streaming_chat_completion_is_transparent`: its payload model is
`gpt-6.1-sol`, which already equals `coding_route.targets[0].model`, so
`json.loads(upstream_request.content) == inbound_payload` (`tests/test_proxy.py:217`) would still
pass. Backward compatibility therefore did **not** compel the current behaviour. The decision was
to preserve transparency (relay the caller's body unchanged); the 1.3 spec is silent on model
translation. The behaviour is **unchanged** — this question is escalated to the user separately,
and a future task may add an explicit model-rewrite step.

### (b) NIT finding 2 — misleading "failing over" log on the last target

**File / lines.** `src/gateway/engine/router.py` — `_log_failover` (now `:139-168`), called from
the exception path at `:253` and the retryable-status path at `:260`.

**Problem.** The single warning was emitted for *every* failed attempt, including the last target
where `remaining == 0`. It therefore printed `… failed (HTTP 429); failing over. 0 target(s)
remaining.` immediately before `All 2 upstream target(s) for route failed; returning HTTP 429.` —
claiming a failover that never happens.

**BEFORE**
```python
def _log_failover(
    target: RouteTargetConfig,
    reason: str,
    remaining: int,
) -> None:
    """Log one failover decision at WARNING level."""
    logger.warning(
        "Upstream target '%s' (model '%s') failed (%s); failing over. "
        "%d target(s) remaining.",
        target.provider,
        target.model,
        reason,
        remaining,
    )
```

**AFTER**
```python
def _log_failover(
    target: RouteTargetConfig,
    reason: str,
    remaining: int,
) -> None:
    """Log one failed attempt at WARNING level.

    The wording distinguishes a real failover (``remaining > 0``, so the next
    target will actually be tried) from the final attempt (``remaining == 0``,
    where the route is about to be exhausted and there is nothing left to fail
    over to). A gateway's logs must never claim a failover that does not happen,
    so the "failing over" phrasing is used only when a further target remains.
    """
    if remaining > 0:
        logger.warning(
            "Upstream target '%s' (model '%s') failed (%s); failing over to the "
            "next target. %d target(s) remaining.",
            target.provider,
            target.model,
            reason,
            remaining,
        )
        return
    logger.warning(
        "Upstream target '%s' (model '%s') failed (%s); no target remains "
        "(last target in the route).",
        target.provider,
        target.model,
        reason,
    )
```

Both branches stay at **WARNING** level; the existing `All N upstream target(s) … failed` summary
line (`router.py:268-272`) is unchanged. The same helper is used by both the status path and the
exception path, so the two failure kinds behave identically.

**Verbatim evidence — captured real `LogRecord`s** (a `logging.Handler` attached to
`gateway.engine.router`, `record.levelname` + `record.getMessage()`):

```
CASE A (non-last target fails) -> HTTP 200
   [WARNING] Upstream target 'sharellm_primary' (model 'gpt-6.1-sol') failed (HTTP 429); failing over to the next target. 1 target(s) remaining.
CASE B (last target fails) -> HTTP 429 | body: {'error': {'message': 'All upstream targets failed', 'type': 'gateway_failover_exhausted'}}
   [WARNING] Upstream target 'sharellm_primary' (model 'gpt-6.1-sol') failed (HTTP 429); failing over to the next target. 1 target(s) remaining.
   [WARNING] Upstream target 'openrouter_backup' (model 'gpt-6-astra') failed (HTTP 429); no target remains (last target in the route).
   [WARNING] All 2 upstream target(s) for route failed; returning HTTP 429.
CASE C (last target times out) -> HTTP 504
   [WARNING] Upstream target 'sharellm_primary' (model 'gpt-6.1-sol') failed (ReadTimeout: boom); failing over to the next target. 1 target(s) remaining.
   [WARNING] Upstream target 'openrouter_backup' (model 'gpt-6-astra') failed (ReadTimeout: boom); no target remains (last target in the route).
   [WARNING] All 2 upstream target(s) for route failed; returning HTTP 504.
```

CASE A (non-last) still says "failing over"; CASE B and CASE C (last, on both the status and the
exception path) say "no target remains (last target in the route)" and never claim a failover.

### (b) verification — no behavioural change

Full suite from the project root:

```
============================= 25 passed in 3.57s ==============================
```
(3 config + 9 proxy + 13 failover.) Isolation re-checked: `test_failover.py` alone → `13 passed in
0.83s`; `test_config.py` alone → `3 passed in 2.17s`. `tests/test_config.py` SHA-256 still
`fd203d6c652d9c523c2f6840e7e45bd9e08cf347aad2e548ed5152b77c1a2f90` (4859 bytes).

Failover probe paths re-run through the real ASGI app — same outcomes as before the fix:

```
(1) buffered 429->200 : status=200 body_ok=True primary_calls=1 backup_calls=1
(2) streaming 502->SSE: status=200 ct=text/event-stream; charset=utf-8 byte_exact=True primary_calls=1 backup_calls=1 backup_closed=True
(3) timeout->200      : status=200 body_ok=True primary_calls=1 backup_calls=1
(4) all-429 envelope  : status=429 body={'error': {'message': 'All upstream targets failed', 'type': 'gateway_failover_exhausted'}}
(5) cooldown skip     : r1=200 r2=200 primary_calls=1 backup_calls=2
```

All five match the pre-fix outcomes (V1–V7 above): buffered failover to 200, byte-exact SSE
failover with the backup stream closed, timeout failover to 200, the 429
`gateway_failover_exhausted` envelope on exhaustion, and the cooldown skip (primary called once
across both requests, backup twice).

### Files changed in round 1

| Path | Change |
| --- | --- |
| `src/gateway/engine/router.py` | Finding 2: `_log_failover` (`:139-168`) now branches on `remaining`, emitting "failing over to the next target" only when a further target remains and a distinct "no target remains (last target in the route)" warning otherwise. Docstring added. No other code touched. |
| `analysis/builder_handoff_1_3.md` | (a) *Known limitations* #2 rewritten to withdraw the "forced" claim; this `## FIXES APPLIED (round 1)` section added. |
