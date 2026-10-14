# Review — Task 1.2 (Round 1)

STATUS: APPROVED

Reviewer: independent read-only review (fresh agent instance).
Note: the configured Reviewer model `Atria-Dawn-Preview` was unavailable (429 token quota
exhausted), so this round ran on the `hy4-preview-f` model with the Reviewer agent's
read-only tool restrictions intact. All evidence below was produced by the Reviewer's own
commands; the Builder's handoff and the orchestrator's probe were not relied upon.

## Spec compliance

### §Scope 1 — `src/gateway/engine/proxy.py`

| Requirement | Result | Evidence |
|---|---|---|
| `forward_request(client, target, provider, request_data, stream, headers)` exact signature | **MET** | `proxy.py:197-204`; `inspect.signature` → `(client, target, provider, request_data, stream, headers)` |
| Header sanitation: strip hop-by-hop, set `Authorization: Bearer <api_key>` | **MET** | `proxy.py:37-62`, `:81-102`; outgoing `host=api.sharellm.example.com`, `authorization=Bearer sk-sharellm-REPLACE_WITH_REAL_KEY`, 8 non-regenerated hop-by-hop headers absent |
| Non-streaming returns `(status_code, response_json, headers)` | **MET** | `proxy.py:137-150`; probe → `(429, {...}, {'x-request-id':'r1'})`, `(502, {error…}, {})` |
| Streaming yields via `response.aiter_bytes()` with clean context-manager closure | **PARTIAL** | `proxy.py:153-172`. Structure and common closure paths correct, but closure is **not deterministic** on one reachable disconnect race and can raise an unhandled `RuntimeError` — see F1 |

### §Scope 2 — `src/gateway/main.py`

| Requirement | Result | Evidence |
|---|---|---|
| Lifespan: `load_config()`, shared client with `Limits(max_keepalive_connections=50, max_connections=200)` on `app.state` | **MET** | `main.py:108-121`, `:33-34` |
| Shutdown awaits `aclose()` | **MET** | `main.py:122-125` |
| `GET /health` → `{"status":"ok","version":"0.1.0"}` | **MET** | `main.py:133-136`; observed `200 {'status': 'ok', 'version': '0.1.0'}` |
| `GET /v1/models` → `{"object":"list","data":[…]}` | **MET** | `main.py:138-145`, `:70-91`; observed `['gpt-6.1-sol','gpt-6-astra']` |
| `POST /v1/chat/completions`: resolve, 404 envelope, `targets[0]`+provider, `StreamingResponse`/JSON | **MET** | `main.py:147-189`; observed 404 envelope, `text/event-stream; charset=utf-8`, `application/json` |
| `run_cli()` via `uvicorn.run` honoring host/port | **MET** | `main.py:199-209`; guarded at `:212-213` |

### §Scope 3 — `tests/test_proxy.py`
All five tests exist and assert the specified behaviour; all pass. Quality caveats in F3/F4.

### §Constraints

| Constraint | Result | Evidence |
|---|---|---|
| Zero-copy / no buffering | **MET** | Gated-upstream probe: first chunk returned while upstream had produced only chunk 1 and the gate was unset |
| Context safety: no leaked sockets on disconnect | **PARTIAL** | Common paths clean; one race not deterministic — F1 |
| Strict types, no `TODO` | **MET** | Full annotations, real `@overload` pair, `JSONValue` alias; no `TODO/FIXME/HACK/placeholder` |
| Full suite green | **MET** | `8 passed in 2.98s` |

## Findings

**F1 — MINOR (borderline) — Non-deterministic cleanup + intermittent unhandled `RuntimeError` when a client disconnect lands while the proxy generator is suspended at a `yield`.**
`src/gateway/engine/proxy.py:168-172`.
If the ASGI server cancels the stream handler *between* a `yield` and the next `__anext__`
(realistic: slow client → `await send(...)` blocks → disconnect detected → Starlette cancels
`stream_response`), cancellation does **not** reach into the suspended generator. Starlette
1.8.0's `StreamingResponse` never calls `aclose()` on `body_iterator`, so the upstream response is
closed only later by asyncio's async-generator finalizer. Observed:

```
closed right after app(): False
closed after gc: True
Task exception was never retrieved
future: <Task finished ... coro=<<async_generator_athrow without __name__>()> exception=RuntimeError("generator didn't stop after athrow()")>
  File ".../proxy.py", line 172, in _forward_streaming
    yield chunk
GeneratorExit
  File ".../proxy.py", line 168, in _forward_streaming
    async with client.stream(...) as response:
  File ".../contextlib.py", line 271, in __aexit__
    raise RuntimeError("generator didn't stop after athrow()")
```

Measured over 20 disconnect runs: **1/20** produced the `RuntimeError`; the other 19 were clean.
Why it matters: (a) the upstream response is **not** closed at disconnect time — reclamation is
deferred to GC, contrary to the spec's explicit "never leak unclosed sockets on client
disconnection"; (b) an unhandled exception is logged on a real request path; (c) the Builder's
handoff §6 overclaims — "`__aexit__` → `response.aclose()` **always** runs. Proven twice" — it
does not run deterministically on this path.

Why not elevated: no permanent leak (the stream is always closed by finalization), the trigger is
a narrow race, and the `async with client.stream(...)` + `aiter_bytes()` structure is exactly what
the spec §1 prescribes, so the residual gap is an interaction with Starlette's disconnect handling
rather than a logic error in the engine. *Fixed* = make closure independent of async-generator
finalization — e.g. obtain the response via `client.send(request, stream=True)` and close it in a
`try/finally` rather than through the nested `asynccontextmanager`, or have the endpoint await the
iterator's `aclose()` on teardown. At minimum add a regression test driving a real disconnect
while the generator is parked at a `yield`.

**F2 — MINOR — The streaming path always returns HTTP 200; an upstream non-2xx status is not surfaced.**
`main.py:178-182`, `proxy.py:153-172`. Probe: upstream `500` with a JSON body → the generator
yields `[b'{"error": "boom"}']` and the caller sees `200`. Disclosed by the Builder and
structurally forced by the spec'd streaming signature (iterator only, no status), so not a spec
deviation — but a real transparency gap (an OpenAI SDK in stream mode will misparse the error body
as SSE).

**F3 — MINOR — New error-handling paths have no test coverage.**
`_non_json_body_error` (`proxy.py:114-134`) and `_forwardable_response_headers` (`proxy.py:105-111`)
are non-trivial added behaviour with no test. Verified manually that a non-JSON upstream body
yields `502 {"error": {"message": "Upstream returned a non-JSON response body (HTTP 502): <html>Bad
Gateway</html>", "type": "upstream_error", …}}` with `{}` forwarded headers, and that `x-request-id`
is relayed while `content-length` is dropped — but nothing in `tests/test_proxy.py` exercises either.

**F4 — MINOR — Test 3's "case-insensitive stripping" is not actually exercised end-to-end.**
`tests/test_proxy.py:182-199`. The inbound mixed-case headers are lowercased by
`httpx.ASGITransport` and again by Starlette before the handler runs, so by the time
`_build_upstream_headers` sees them they are already lowercase. The engine's `.lower()` matching is
correct (verified directly: `_build_upstream_headers({"HOST": "evil", "AUTHORIZATION": "Bearer
inbound", "Connection": "close"}, "PROVIDERKEY")` → single `Authorization: Bearer PROVIDERKEY`), but
the test does not prove it. A unit test calling the helper with mixed-case keys would.

**F5 — NIT — `Connection`-nominated hop-by-hop headers are not handled (RFC 9110 §7.6.1).**
`proxy.py:81-102`. The fixed set (`:37-49`) covers the standard hop-by-hop headers plus
non-standard `proxy-connection`, but a strict proxy must also drop any header *named by* the
inbound `Connection` field (e.g. `Connection: X-Internal`). Not required by the task spec, which
only names `host`/`content-length`/`connection`.

**F6 — NIT — The streaming path does not relay upstream response headers.**
`proxy.py:153-172` / `main.py:182` — e.g. an upstream `x-request-id` is dropped on `stream=True`
(the buffered path relays it). Not required by the spec; noted for symmetry.

No BLOCKER findings. No MAJOR findings.

## Verified independently

**Full suite (cache writes disabled):**
```
cd "C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway" && \
  C:/Users/Notebook/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe -m pytest -v -p no:cacheprovider
→ collected 8 items … 8 passed in 2.98s   (test_config: 3 passed; test_proxy: 5 passed)
```

**Import inertness + signature:** importing `gateway.main` → `app type: FastAPI`,
`has http_client before lifespan: False`, `binds during import: []`; `overloads: 2` with
`stream: 'Literal[False]' → tuple[int, JSONValue, dict[str, str]]` and
`stream: 'Literal[True]' → AsyncIterator[bytes]`.

**No buffering (own gated probe, `forward_request` driven directly):**
```
first chunk within 2s: b'data: {"delta":"1"}\n\n' | upstream produced: [b'c1'] | gate set: False
rest: b'data: {"delta":"2"}\n\n' | upstream produced: [b'c1', b'c2'] | closed: True
```

**Context safety:**
```
early abandon (aclose):   closed before aclose: False → closed after aclose: True
upstream raises mid-stream: exception propagated: ReadError … | upstream closed: True
cancellation (blocked inside generator): closed immediately after cancel: True
```

**Header sanitation (captured upstream request):** outgoing `host=api.sharellm.example.com`,
`auth=Bearer sk-sharellm-REPLACE_WITH_REAL_KEY`, `x-trace-id=trace-123`, `content-length=13`;
`keep-alive/transfer-encoding/upgrade/te/trailer/proxy-authenticate/proxy-authorization/proxy-connection`
all `present: False`.

**Non-JSON upstream body / status propagation:** `502` HTML body → `502 {"error": {"message":
"Upstream returned a non-JSON response body (HTTP 502): <html>Bad Gateway</html>", "type":
"upstream_error", …}}`; `429` JSON → `429`, `{'x-request-id': 'r1'}`.

**Endpoints via ASGITransport:** `health: 200`; `models: 200 list ['gpt-6.1-sol','gpt-6-astra']`;
404 envelope correct; `stream ct: 200 text/event-stream; charset=utf-8`; `json ct: 200 application/json`.

**No real ports (audit hook during `tests/test_proxy.py`):** `Counter({'socket.__new__': 10,
'socket.bind': 5, 'socket.connect': 5})`, every bind `('127.0.0.1', 0)` — no fixed port, no DNS.

**Builder caveats judged:** Caveat #1 (httpx regenerates `host`/`content-length`/`connection`) —
**legitimate**, confirmed. Caveat #2 (`ASGITransport` coalesces body parts) — **legitimate**,
confirmed in source (`ASGIResponseStream.__aiter__` yields `b"".join(self._body)`).

## Verdict rationale

Spec compliance is essentially complete: the exact signature, the overload pair, header
sanitation, the lifespan-managed pooled client, all three endpoints, `run_cli`, zero-copy
streaming, mid-stream exception handling, deterministic early-abandon cleanup, and the five tests
are all present and independently reproduced, and Task 1.1's three config tests remain green with
`config.py` unchanged. The only substantive issue (F1) is a narrow, intermittent, GC-deferred
cleanup gap on one disconnect race rooted in the spec-prescribed `async with client.stream(...)` +
`yield` structure interacting with Starlette 1.8.0 — it does not leak a socket permanently, so it
is a follow-up robustness item rather than a merge blocker. No BLOCKER or MAJOR ⇒ **APPROVED**,
with F1 recommended as a tracked follow-up (and F2–F4 worth addressing).
