# Builder Handoff — Task 1.2

STATUS: CHANGES_APPLIED (round 1 — see `## FIXES APPLIED (round 1)` at the bottom)

Task: **Core Transparent Reverse Proxy & Streaming Engine** for the ShareLLM Smart Gateway.
Implemented the proxy forwarding engine (`src/gateway/engine/proxy.py`), the FastAPI
application (`src/gateway/main.py`) and the proxy test suite (`tests/test_proxy.py`).
Full suite: **12 passed** (3 Task 1.1 config tests + 9 proxy tests). No Task 1.1 file was
modified.

> **Round-1 note.** The original text below is kept as-is except where this round proved it
> wrong. Two places were corrected in place: §6 of *Key design decisions* and *Verification
> V4* — both previously claimed stream cleanup "always" happened. That claim was false for the
> client-disconnect path (finding F1). See `## FIXES APPLIED (round 1)` for the correction,
> the fix and the evidence.

---

## Files created / changed (absolute paths)

| Path | Purpose |
| --- | --- |
| `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway/src/gateway/engine/proxy.py` | **Created.** Transparent proxy engine: header sanitation, buffered (`stream=False`) and streaming (`stream=True`) forwarding, dual-return `forward_request` typed with `@overload`. |
| `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway/src/gateway/main.py` | **Created.** FastAPI app factory + module-level `app`: lifespan (shared pooled `httpx.AsyncClient`), `/health`, `/v1/models`, `/v1/chat/completions`, `run_cli()`. |
| `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway/tests/test_proxy.py` | **Created.** The five required proxy tests, in-process via `httpx.ASGITransport` + `respx`; **round 1 added 4 more** (F1 disconnect regression, F3 ×2, F4/F5 header sanitation). |

Nothing else was touched. `src/gateway/config.py`, `src/gateway/__init__.py`,
`config.example.yaml`, `pyproject.toml` and `tests/test_config.py` are **byte-for-byte
unchanged** — no Task 1.1 bug was encountered. No files outside the project root were
created or modified (all scratch probes were run via stdin heredocs, never written to disk).

---

## Key design decisions

### 1. Driving the lifespan in tests (no `asgi-lifespan`, no socket)

`httpx.ASGITransport` does **not** run the ASGI lifespan, so `app.state.http_client` /
`app.state.app_config` would not exist. The `gateway` fixture drives it explicitly:

```python
application = create_app(EXAMPLE_CONFIG_PATH)
async with application.router.lifespan_context(application):   # <-- startup/shutdown run
    assert isinstance(application.state.http_client, httpx.AsyncClient)
    assert application.state.app_config is not None
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url=GATEWAY_BASE_URL) as client:
        yield GatewayHarness(app=application, client=client)
```

Verified empirically before committing to it: `app.state` is populated inside the context and
released after it (see *Verification E1*).

### 2. `pytest-asyncio` strict mode

`asyncio_mode` is left at the environment's `STRICT`. `pyproject.toml` is **not** modified;
instead the async fixture uses `@pytest_asyncio.fixture` and every async test carries an
explicit `@pytest.mark.asyncio`. The suite collects exactly **8** items — the async tests are
collected and run, not silently skipped (see the `collected 8 items` line below).

### 3. `create_app(config_path: str | Path | None = None)`

`load_config()` defaults to `config.yaml`, which does not exist in this repo, so the tests must
not depend on it. `create_app` closes over an optional `config_path`:

```python
config = load_config(config_path) if config_path is not None else load_config()
```

* `config_path=None` (the production default, and the module-level `app = create_app()`) calls
  `load_config()` with no argument — exactly the spec's "load `AppConfig` via `load_config()`".
* Tests call `create_app(EXAMPLE_CONFIG_PATH)` so the suite never needs `config.yaml`.

Verified: `create_app()` with no path defers to `load_config()` and, because `config.yaml` is
absent, raises `FileNotFoundError` **only when the lifespan runs** — importing the module is
inert (*Verification E6*).

### 4. Dual-return `forward_request` — `typing.overload` on `Literal[True]` / `Literal[False]`

The public signature is exactly the spec's, with real types:

```
(client: httpx.AsyncClient, target: RouteTargetConfig, provider: ProviderConfig,
 request_data: dict[str, Any], stream: bool, headers: dict[str, str])
 -> tuple[int, JSONValue, dict[str, str]] | AsyncIterator[bytes]
```

with two registered overloads:

```
stream: Literal[False] -> tuple[int, JSONValue, dict[str, str]]
stream: Literal[True]  -> AsyncIterator[bytes]
```

`forward_request` stays an `async def`; for the streaming case it **returns** the async
generator produced by `_forward_streaming(...)` (the request is therefore only sent when the
consumer starts iterating — verified in *E4*). The endpoint passes the literals `True` / `False`
explicitly so each call site resolves to a single overload.

`JSONValue` (`dict[str, Any] | list[Any] | str | int | float | bool | None`) is used instead of
a bare `Any` for the buffered body. `Any` only appears *inside* that recursive JSON alias and in
`dict[str, Any]` payloads, where heterogeneous JSON genuinely requires it.

### 5. Header sanitation — exactly what is stripped and why

Request headers (case-insensitive, via `name.lower()`):

| Stripped | Why |
| --- | --- |
| `connection`, `keep-alive`, `transfer-encoding`, `upgrade`, `te`, `trailer`, `proxy-authenticate`, `proxy-authorization`, `proxy-connection` | Hop-by-hop (RFC 9110 §7.6.1 / RFC 7230 §6.1): they describe one transport hop only; forwarding them corrupts the next hop's framing. `proxy-connection` is the non-standard variant. |
| `host` | Describes the *original* hop; the forwarding client must emit the upstream authority. |
| `content-length` | The body is re-serialised, so the original framing is wrong. |
| `authorization` | Replaced with `Authorization: Bearer <provider.api_key>`. |

Response headers: the same hop-by-hop set plus `content-length`, `content-encoding`,
`content-type` — the body is re-serialised as JSON by the endpoint, so the upstream framing and
content type no longer describe it. Everything else (e.g. `x-request-id`) is relayed verbatim.

### 6. Stream cleanup guarantee

> **CORRECTED IN ROUND 1 (finding F1).** The original wording of this section claimed the
> cleanup "always runs" and that `__aexit__` was sufficient. That was **wrong** for the
> client-disconnect path, and the code has since been restructured. The original text is kept
> below for the record; the accurate statement is in `## FIXES APPLIED (round 1)`.

*Original (superseded) text:*

```python
async def _forward_streaming(client, url, request_data, headers, timeout) -> AsyncIterator[bytes]:
    async with client.stream("POST", url, json=request_data, headers=headers, timeout=timeout) as response:
        async for chunk in response.aiter_bytes():
            yield chunk
```

The `async with` owns the upstream response inside the async generator. Abandoning the stream
(`aclose()`, or a client disconnect raising `GeneratorExit` at the `yield`) propagates out of
the `async with`, so `__aexit__` → `response.aclose()` **always** runs. Proven twice: inside
test 4 (part C) and standalone (*E5*).

**Accurate position after round 1:** the above is true only when the *consumer* closes the
iterator (`aclose()`), or when the generator is finalized by GC. It is **not** true on the
client-disconnect path: Starlette 1.8.0's `StreamingResponse` never calls `aclose()` on
`body_iterator` (`starlette/responses.py:251-258`), so on disconnect the generator is simply
abandoned at its `yield` and reclaimed later by the async-generator finalizer — and closing it
that way raised `RuntimeError("generator didn't stop after athrow()")` in 1/40 runs. The
streaming path was therefore restructured (`client.send(..., stream=True)` + `try/finally`) and
the response layer now owns the iterator's lifetime, which makes release deterministic on all
four exit paths. See `## FIXES APPLIED (round 1)` → F1.

### 7. No buffering

`response.aiter_bytes()` is iterated and each chunk is yielded immediately; nothing calls
`aread()` and no list of chunks is accumulated. Proven with a **gated** upstream stream whose
second chunk is withheld until an external `asyncio.Event` is set: the first proxy chunk is
returned while the upstream has produced only chunk 1 (*E4*).

### 8. Model de-duplication (`GET /v1/models`)

`_aggregate_models` walks `config.routes` in declaration order and appends each alias only if it
has not been seen (`seen: set[str]`), preserving first-seen order. In `config.example.yaml`
`gpt-6.1-sol` is served by **both** `coding_route` and `chat_route`, so the result is exactly
`["gpt-6.1-sol", "gpt-6-astra"]` (2 entries). The test *derives* this list from
`config.example.yaml` via `load_config` **and** pins the literal set `{"gpt-6.1-sol",
"gpt-6-astra"}` / `len == 2`.

### 9. Per-target timeout

`forward_request` applies `httpx.Timeout(target.timeout_seconds)` per request — this is the
reason `RouteTargetConfig.timeout_seconds` exists. The lifespan client carries a fallback
overall timeout of `httpx.Timeout(60.0)`.

---

## Verification evidence

Environment: `C:/Users/Notebook/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe`
(Python 3.13.14, pytest 9.1.1, pytest-asyncio 1.4.0, respx 0.23.1, httpx 0.28.1, fastapi
0.143.1, starlette 1.8.0). Nothing was installed.

### V1 — full suite from the project root (the required command)

```
cd "C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway" && C:/Users/Notebook/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe -m pytest -v
```

```
============================= test session starts =============================
platform win32 -- Python 3.13.14, pytest-9.1.1, pluggy-1.6.0 -- C:\Users\Notebook\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe
cachedir: .pytest_cache
rootdir: C:\Users\Notebook\WorkBuddy AI\2026-10-14-19-08-04\sharellm-gateway
configfile: pyproject.toml
testpaths: tests
plugins: anyio-4.15.1, asyncio-1.4.0, respx-0.23.1
asyncio: mode=Mode.STRICT, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 8 items

tests/test_config.py::test_example_config_loads_and_resolves_references PASSED [ 12%]
tests/test_config.py::test_unknown_provider_reference_raises PASSED      [ 25%]
tests/test_config.py::test_minimal_config_applies_documented_defaults PASSED [ 37%]
tests/test_proxy.py::test_health_endpoint PASSED                         [ 50%]
tests/test_proxy.py::test_models_endpoint_aggregates_and_deduplicates PASSED [ 62%]
tests/test_proxy.py::test_non_streaming_chat_completion_is_transparent PASSED [ 75%]
tests/test_proxy.py::test_streaming_chat_completion_forwards_sse_chunks PASSED [ 87%]
tests/test_proxy.py::test_unmapped_model_returns_404 PASSED              [100%]

============================== 8 passed in 2.63s ==============================
```

Summary line: **`8 passed in 2.63s`** — collected 8 items (3 config + 5 proxy), all async tests
executed. Task 1.1's three config tests are green.

### V2 — the suite opens no real network ports

`sys.addaudithook` recording every `socket.*` event while `tests/test_proxy.py` runs:

```
('socket.__new__', '(<socket.socket fd=-1, family=0, type=0, proto=0>, 2, 1, 0)')
('socket.bind', "(<socket.socket fd=1536, family=2, type=1, proto=0>, ('127.0.0.1', 0))")
('socket.connect', "(<socket.socket fd=1616, family=2, type=1, proto=0>, ('127.0.0.1', 57110))")
... (5 identical bind/connect pairs, one per async test's event loop) ...
total socket.* events: 20
```

Every event is `127.0.0.1` bound to **port 0** (kernel-assigned ephemeral) followed by a
connect to that ephemeral port — that is the Windows `ProactorEventLoop` self-pipe created by
`socket.socketpair()`, not the gateway. There is **no `socket.getaddrinfo`**, no hostname
resolution and no bind to a fixed port.

Corroborating run with `socket.getaddrinfo` **and** `uvicorn.run` replaced by functions that
raise:

```
.....                                                                    [100%]
5 passed, 1 warning in 0.55s
PYTEST_EXIT 0
no DNS resolution and no uvicorn.run occurred during the suite
```

### V3 — no buffering (incremental delivery), gated upstream

An upstream `AsyncByteStream` that withholds chunk 2 behind an `asyncio.Event`, driven through
the real `forward_request`:

```
first chunk returned while chunk-2 is still gated: b'data: 1\n\n'
upstream log at that moment: ['produced chunk-1']
second chunk after opening the gate: b'data: 2\n\n'
upstream log after aclose: ['produced chunk-1', 'produced chunk-2', 'aclose() called']
upstream stream closed: True
```

The first chunk is returned under a 2 s `asyncio.wait_for` while the upstream has produced only
chunk 1 — a buffering implementation would have hung and timed out.

### V4 — context safety / no socket leak on early consumer exit

```
first = b'a' | released = [b'a'] | closed = False
after agen.aclose(): stream.closed = True
```

and the same probe against a `RecordingByteStream` upstream inside the suite (test 4, part C):

```python
assert await iterator.__anext__() == SSE_CHUNKS[0]
assert abandoned_stream.closed is False
await iterator.aclose()               # consumer abandons the stream
assert abandoned_stream.closed is True
```

`aclose()` on the proxy generator runs the generator's cleanup (`async with client.stream(...)`
`__aexit__` at the time; the `try/finally` since round 1), which closes the upstream stream — no
leaked socket **on this path**.

> **Scope correction (round 1, finding F1).** V4 covers only the *consumer-driven* exit. It does
> **not** cover the client-disconnect path, where nothing calls `aclose()` on the iterator and
> the old code therefore leaked the stream past the disconnect (closed only at GC time, with an
> intermittent `RuntimeError`). That path is fixed and now covered by its own regression test —
> see `## FIXES APPLIED (round 1)` → F1.

### V5 — `forward_request` public shape and overloads

```
public signature: (client: 'httpx.AsyncClient', target: 'RouteTargetConfig', provider: 'ProviderConfig', request_data: 'dict[str, Any]', stream: 'bool', headers: 'dict[str, str]') -> 'tuple[int, JSONValue, dict[str, str]] | AsyncIterator[bytes]'
parameter names : ['client', 'target', 'provider', 'request_data', 'stream', 'headers']
registered overloads: 2
   ... stream: 'Literal[False]' ... -> tuple[int, dict[str, Any] | list[Any] | str | int | float | bool | None, dict[str, str]]
   ... stream: 'Literal[True]'  ... -> typing.AsyncIterator[bytes]
```

### V6 — importing `gateway.main` is inert

```
imported gateway.main; app = FastAPI
has http_client before lifespan: False
has app_config before lifespan: False
routes: ['/docs', '/docs/oauth2-redirect', '/health', '/openapi.json', '/redoc', '/v1/chat/completions', '/v1/models']
run_cli callable: True
has __main__ guard: True
create_app() with no path -> load_config() default -> FileNotFoundError
  message: Configuration file not found: C:\Users\Notebook\WorkBuddy AI\2026-10-14-19-08-04\sharellm-gateway\config.yaml (expected an existing YAML file).
```

No port is bound, no task is spawned, no config file is read at import time.

### V7 — non-JSON upstream body does not raise; response headers are filtered

```
non-JSON upstream -> status: 502
non-JSON upstream -> body: {"error": {"message": "Upstream returned a non-JSON response body (HTTP 502): <html>Bad Gateway</html>", "type": "upstream_error", "param": null, "code": null}}
non-JSON upstream -> forwarded response headers: {}
JSON upstream -> status: 200 | body: {'ok': True}
JSON upstream -> forwarded response headers: {'x-request-id': 'req-1'}
```

### V8 — header sanitation observed on the request respx actually captured

The upstream request captured by respx in test 3 (with the inbound request spoofing
`Host: spoofed-gateway.example`, `Content-Length: 9999`, `Connection: close`, plus
`keep-alive`, `transfer-encoding`, `upgrade`, `te`, `trailer`, `proxy-authenticate`,
`proxy-authorization`, `proxy-connection`, and `Authorization: Bearer inbound-caller-token`):

```
Headers({'host': 'api.sharellm.example.com', 'connection': 'keep-alive', 'accept': '*/*',
         'accept-encoding': 'gzip, deflate', 'user-agent': 'python-httpx/0.28.1',
         'x-trace-id': 'trace-123', 'content-type': 'application/json',
         'authorization': '[secure]', 'content-length': '82'})
```

* `authorization` = `Bearer sk-sharellm-REPLACE_WITH_REAL_KEY` (asserted; the inbound caller
  token was replaced).
* `x-trace-id: trace-123` survived the hop.
* `keep-alive`, `transfer-encoding`, `upgrade`, `te`, `trailer`, `proxy-authenticate`,
  `proxy-authorization`, `proxy-connection` are **absent** — dropped outright.
* `host` = `api.sharellm.example.com` (not the spoofed `spoofed-gateway.example`),
  `content-length` = `82` = `len(forwarded body)` (not `9999`), `connection` = `keep-alive`
  (not the inbound `close`).

---

## Known limitations / follow-ups

1. **`host` / `content-length` / `connection` are regenerated by httpx and therefore always
   present on the outgoing request.** This is a spec nuance, not a gap: httpx sets these on
   every request it sends (the default `Connection: keep-alive` lives at
   `httpx/_client.py:311`; `Host` and `Content-Length` are derived from the URL and body). A
   correct HTTP client *must* send them. The task's "did NOT carry host/content-length/
   connection" is therefore tested as "the **inbound** values never leaked through": the test
   spoofs distinctive inbound values (`spoofed-gateway.example`, `9999`, `close`) and asserts
   the captured upstream request carries the regenerated, correct values instead. The other
   eight hop-by-hop headers (which httpx does *not* regenerate) are asserted **absent**, which
   is the literal form of the requirement. A proxy that naively forwarded
   `dict(request.headers)` would fail all of these assertions.
2. **`httpx.ASGITransport` coalesces the response body.** `ASGIResponseStream.__aiter__` yields
   `b"".join(self._body)` (`httpx/_transports/asgi.py:60`), so per-chunk boundaries are not
   observable through the ASGI client even though the gateway streams them. Test 4 therefore
   proves per-chunk, in-order relay at the engine boundary (driving `forward_request` directly
   and asserting the yielded chunks equal `SSE_CHUNKS` exactly) and proves end-to-end fidelity
   through the ASGI app by byte-equality plus ordered SSE-event parsing. No real server is used
   because the task forbids opening ports.
3. **Failover to backup targets is NOT part of this task.** Only the primary target
   `route.targets[0]` is used; `openrouter_backup` / the failover `strategy` are never consulted.
   Retry, timeouts-across-targets, round-robin and adapter selection are out of scope.
4. **Streaming responses always return HTTP 200 to the caller; an upstream non-2xx status is
   not surfaced (reviewer finding F2 — deliberately not fixed this round).**
   *Cause:* the task spec prescribes a streaming `forward_request` that returns **only** an async
   byte iterator (no status, no headers), so the endpoint has nothing to build a status code
   from; the upstream error body is relayed verbatim inside a 200 SSE response. An OpenAI SDK in
   stream mode will therefore try to parse an error body as SSE.
   *What a future task would need to change:* widen the spec'd streaming return shape — e.g.
   return a small handle carrying `status_code` + `headers` + the byte iterator (or have
   `forward_request` raise a typed `UpstreamError(status, body)` that the endpoint turns into a
   normal JSON error response *before* the first byte is sent). Both are breaking changes to the
   signature the spec pins, so they are out of scope here. The buffered path already propagates
   the upstream status code correctly.
5. **The streaming path does not relay upstream response headers (reviewer finding F6 —
   deliberately not fixed this round).**
   *Cause:* same as F2 — the spec'd streaming return is an iterator only, so there is no channel
   for the upstream headers (an upstream `x-request-id` reaches the caller on the buffered path
   but is dropped on `stream=True`). *What a future task would need to change:* the same widened
   return shape as F2 (status + headers + iterator), after which the endpoint can pass
   `_forwardable_response_headers(...)` into the `StreamingResponse` exactly as the buffered path
   does today.
6. **`created` in `/v1/models` is `int(time.time())` at request time** (standard OpenAI
   behaviour; there is no real per-alias creation date for an aggregated gateway). The test
   asserts the type, not a fixed instant. `owned_by` is the constant `"sharellm-gateway"`.
7. **`run_cli()` reads the config file twice** — once for `gateway.host`/`gateway.port`/
   `log_level` and once inside the lifespan. This keeps the spec's "lifespan loads the config
   via `load_config()`" intact; the cost is one extra YAML read at boot.
8. **`mypy` / `ruff` are not installed** in the provided environment, so no static-analysis run
   was performed (typing is enforced by full annotations, the `@overload` pair and Pydantic).
   `mypy` was confirmed absent (`ModuleNotFoundError: No module named 'mypy'`).
9. `httpx.Timeout(target.timeout_seconds)` sets connect/read/write/pool uniformly. For a long
   SSE stream the read timeout is the max idle gap between chunks, which is the intended
   semantic for `timeout_seconds`.

*Superseded entries removed in round 1:* the previous item 4 ("streaming always returns 200")
and item 5 (numbering) are replaced by the two explicit F2/F6 entries above, which now state the
cause and the required future change rather than only the symptom.

---

## EVIDENCE PACKAGE (minimal reviewer reading list)

1. `src/gateway/engine/proxy.py`
2. `src/gateway/main.py`
3. `tests/test_proxy.py`
4. `config.example.yaml` (the fixture the proxy tests assert against — read-only reference)

---

## FIXES APPLIED (round 1)

Round-1 verdict: **APPROVED** (no BLOCKER/MAJOR). Findings F1, F3, F4 and F5 are now closed.
F2 and F6 were explicitly *not* to be fixed — they are documented in *Known limitations* items
4 and 5 above. No existing test was weakened or deleted; `config.py` is untouched.

---

### F1 — MINOR (borderline) — non-deterministic cleanup + intermittent `RuntimeError` on client disconnect

**Step 1 — reproduced first (as instructed).** The disconnect was driven through the real ASGI
app on the `spec_version 2.3` path (the one uvicorn uses), with `send` blocked on the first body
chunk (stalled client) and `receive` reporting `http.disconnect`, so the proxy generator is parked
at a `yield` when the cancellation lands.

**BEFORE the fix** (40 runs, loop exception handler installed):

```
closed at disconnect time: 0 / 40
loop-level exceptions captured: 1 / 40
--- verbatim first captured exception ---
Traceback (most recent call last):
  File "...\src\gateway\engine\proxy.py", line 172, in _forward_streaming
    yield chunk
GeneratorExit

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "...\src\gateway\engine\proxy.py", line 168, in _forward_streaming
    async with client.stream(
               ~~~~~~~~~~~~~^
        "POST", url, json=request_data, headers=headers, timeout=timeout
        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    ) as response:
    ^
  File "...\python\versions\3.13.12\Lib\contextlib.py", line 271, in __aexit__
    raise RuntimeError("generator didn't stop after athrow()")
RuntimeError: generator didn't stop after athrow()
```

A second, independent 20-run pass matched the Reviewer's numbers exactly:
`NOT_closed_after_app: 20 | closed_after_gc: 20 | runtime_error_runs: 1 | clean_runs: 19`.
**Confirmed: the finding is real, and the upstream stream is closed only by GC on this path.**

**Root cause.** Starlette 1.8.0's `StreamingResponse.stream_response` iterates
`async for chunk in self.body_iterator` and never calls `aclose()` on it
(`starlette/responses.py:251-258`). On disconnect it cancels `stream_response` and abandons the
generator at its `yield`; the upstream response was then released only by the async-generator
finalizer. Closing it that way re-enters `contextlib._AsyncGeneratorContextManager.__aexit__` for
the nested `client.stream(...)` context manager, whose `athrow(GeneratorExit)` path can end at
`raise RuntimeError("generator didn't stop after athrow()")` (`contextlib.py:270-273`).

**Step 2 — the fix (both Reviewer directions applied).**

* *Determinism* — the response layer now owns the iterator's lifetime.
  `src/gateway/main.py:49-69` adds `_ReleasingStreamingResponse`, a `StreamingResponse` subclass
  whose `__call__` wraps `super().__call__` in `try/finally` and awaits
  `body_iterator.aclose()`. It is used at `src/gateway/main.py:207`. This closes the iterator on
  **every** exit path — full drain, early stop, and cancellation — before the coroutine returns.
* *No nested context manager* — `src/gateway/engine/proxy.py:173-198` replaces
  `async with client.stream(...)` with an explicit
  `response = await client.send(request, stream=True)` (`proxy.py:193`) plus
  `try/finally: await response.aclose()` (`proxy.py:198`). `forward_request` builds the request at
  `proxy.py:253-256`. This removes the `athrow`-through-`__aexit__` path entirely; the `finally`
  runs the close directly. The public `forward_request` signature is unchanged (`stream: bool`,
  same six parameters, same overload pair — re-verified below).

**AFTER the fix** (identical 40-run probe):

```
AFTER FIX -> closed at disconnect time: 40 / 40
AFTER FIX -> loop-level exceptions : 0 / 40
```

**Regression test.** `tests/test_proxy.py::test_client_disconnect_releases_upstream_stream_at_disconnect_time`
drives the same disconnect 20× through the real ASGI app and asserts, **synchronously and before
any `gc.collect()`**, that `upstream_stream.closed is True` and that exactly one chunk was released
(`released == [SSE_CHUNKS[0]]`, i.e. no read-ahead), plus `loop_errors == []`. The assertion is
made in the same event-loop step in which the app returns, so a GC-deferred close cannot satisfy
it. The test is **discriminating** — re-running it against the pre-fix behaviour (old
`_forward_streaming` + plain `StreamingResponse.__call__`, patched in memory) fails:

```
PRE-FIX: F1 regression test FAILS as expected -> upstream stream leaked past disconnect
```

**All four streaming exit paths now release the upstream response** (standalone probe):

```
1 full drain            : chunks=[b'a', b'b', b'c'] closed=True
2 early aclose()        : first=b'a' closed_before=False closed_after=True
3 upstream raises       : propagated='ReadError: upstream died mid-stream' closed=True
4 server cancellation   : closed_at_disconnect=True released=[b'a']
```

### F3 — MINOR — untested error-handling paths

Two new tests:

* `tests/test_proxy.py::test_non_json_upstream_error_body_is_wrapped` — mocks an upstream
  `502` with an HTML body through the **public endpoint** and asserts the upstream status is
  propagated (`response.status_code == 502`), the body is re-serialised as JSON
  (`content-type: application/json`), and the payload is
  `{"error": {"type": "upstream_error", "param": None, "code": None, "message": ...}}` whose
  message contains both `HTTP 502` and `<html>Bad Gateway</html>`. Nothing raises.
* `tests/test_proxy.py::test_forwardable_response_headers_drop_framing_headers` — unit test on
  `_forwardable_response_headers`: an end-to-end header is relayed
  (`{"x-request-id": "req-1", "x-ratelimit-remaining": "17"}` exactly) while `content-type`,
  `content-encoding`, `connection`, `transfer-encoding` and `content-length` are dropped.

### F4 — MINOR — case-insensitive stripping not exercised

`tests/test_proxy.py::test_request_header_sanitisation_is_case_insensitive_and_honours_connection`
calls `_build_upstream_headers` directly with mixed-case keys (`HOST`, `AUTHORIZATION`,
`Connection`, `Content-Length`, `Keep-Alive`, `Transfer-Encoding`, `Upgrade`, `X-Internal-Trace`,
`X-Trace-Id`) — bypassing `ASGITransport`/Starlette's lower-casing, which is why the end-to-end
test could not prove it. It asserts the hop-by-hop and client-rebuilt headers are gone
case-insensitively, that the end-to-end header survives, and that there is **exactly one**
`Authorization` entry (`authorization_keys == ["Authorization"]`) holding
`Bearer <provider.api_key>` — with the inbound `Bearer inbound-caller-token` value absent.
The existing end-to-end Test 3 is left as-is.

### F5 — NIT — `Connection`-nominated hop-by-hop headers (RFC 9110 §7.6.1)

Implemented in `src/gateway/engine/proxy.py:81-98` as `_connection_nominated_headers`, which
parses the inbound `Connection` field as a comma-separated list and lower-cases each element.
`_build_upstream_headers` (`proxy.py:101-122`) unions that set with the fixed hop-by-hop set
before filtering, so a header named by `Connection` is dropped as well. Covered by the F4 unit
test above: the inbound request carries `Connection: keep-alive, X-Internal-Trace` and the
assertion `"x-internal-trace" not in present` passes.

### Verification after the fixes

Full suite from the project root (the required command):

```
cd "C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway" && C:/Users/Notebook/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe -m pytest -v
```

```
collecting ... collected 12 items

tests/test_config.py::test_example_config_loads_and_resolves_references PASSED [  8%]
tests/test_config.py::test_unknown_provider_reference_raises PASSED      [ 16%]
tests/test_config.py::test_minimal_config_applies_documented_defaults PASSED [ 25%]
tests/test_proxy.py::test_health_endpoint PASSED                         [ 33%]
tests/test_proxy.py::test_models_endpoint_aggregates_and_deduplicates PASSED [ 41%]
tests/test_proxy.py::test_non_streaming_chat_completion_is_transparent PASSED [ 50%]
tests/test_proxy.py::test_streaming_chat_completion_forwards_sse_chunks PASSED [ 58%]
tests/test_proxy.py::test_unmapped_model_returns_404 PASSED              [ 66%]
tests/test_proxy.py::test_request_header_sanitisation_is_case_insensitive_and_honours_connection PASSED [ 75%]
tests/test_proxy.py::test_forwardable_response_headers_drop_framing_headers PASSED [ 83%]
tests/test_proxy.py::test_non_json_upstream_error_body_is_wrapped PASSED [ 91%]
tests/test_proxy.py::test_client_disconnect_releases_upstream_stream_at_disconnect_time PASSED [100%]

============================= 12 passed in 2.73s ==============================
```

Summary line: **`12 passed in 2.73s`** — the original 8 plus 4 new tests. Task 1.1's three config
tests are still green.

**No real ports bound by the suite** (audit hook over `tests/test_proxy.py`):

```
9 passed in 0.91s
PYTEST_EXIT 0
socket.* events: {'socket.__new__': 14, 'socket.bind': 7, 'socket.connect': 7}
every bind address: ["('127.0.0.1', 0)"]
any fixed (non-zero) port bound: False
```

Every bind is `127.0.0.1` on port `0` (the Windows event-loop self-pipe); no fixed port, no DNS.

**No buffering is preserved** (gated-upstream probe re-run after the restructure):

```
no-buffering: first chunk returned while chunk-2 gated: b'data: 1\n\n' | upstream log: ['produced chunk-1']
no-buffering: second chunk after gate: b'data: 2\n\n'
no-buffering: upstream log: ['produced chunk-1', 'produced chunk-2', 'aclose() called'] | closed: True
```

**Signature / import inertness unchanged:**

```
imported gateway.main; app = FastAPI
has http_client before lifespan: False
has app_config before lifespan: False
__main__ guard present: True
forward_request signature: (client, target, provider, request_data, stream, headers) -> 'tuple[int, JSONValue, dict[str, str]] | AsyncIterator[bytes]'
registered overloads: 2
create_app() no path -> load_config() default -> FileNotFoundError
```

No `TODO`/`FIXME`/`HACK`/`placeholder`/`stub`/`mock` markers exist under `src/gateway`.

**Task 1.1 artefacts untouched** (SHA-256 prefix / size, unchanged by this round):

```
cee462a0a5b5fdc6  src/gateway/config.py   (9250 bytes)
bcad860f783d924d  src/gateway/__init__.py (526 bytes)
65e12e3884a7bf25  config.example.yaml     (1442 bytes)
385809c9b63c99a7  pyproject.toml          (717 bytes)
fd203d6c652d9c52  tests/test_config.py    (4859 bytes)
```

Stability: the full suite was run three further times (`12 passed in 2.87s`, `2.78s`, `2.82s`)
and the F1 regression test five times in isolation (`1 passed` each) — no flakiness.

### Files changed in round 1

| Path | Change |
| --- | --- |
| `src/gateway/engine/proxy.py` | F1: `_forward_streaming` now `client.send(..., stream=True)` + `try/finally` (lines 173-198); `forward_request` builds the request (lines 253-256). F5: added `_connection_nominated_headers` (81-98) and folded it into `_build_upstream_headers` (101-122). |
| `src/gateway/main.py` | F1: added `_ReleasingStreamingResponse` (49-69) and used it in the endpoint (207). |
| `tests/test_proxy.py` | F1 regression test, F3 ×2, F4/F5 unit test; private helpers imported for the focused unit tests. |
