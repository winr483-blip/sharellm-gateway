# Review — Task 1.2 (Round 2)

STATUS: APPROVED

Reviewer: independent read-only review (fresh agent instance, no prior context).
Note: the configured Reviewer model `Atria-Dawn-Preview` was unavailable (429 token quota
exhausted), so this round ran on the `hy4-preview-f` model with the Reviewer agent's
read-only tool restrictions intact. All evidence below was produced by the Reviewer's own
probes; neither the Builder's handoff nor the orchestrator's probe was relied upon.

## Round-1 findings adjudication

**F1 — FIXED (verified deterministically, not by a single lucky run).**
The Reviewer wrote its own disconnect harness (real ASGI app via
`create_app(config.example.yaml)`, spec_version 2.3, `send` blocked on the first body chunk,
`receive` returning `http.disconnect` only after that chunk is sent so the generator is parked
at a `yield`, loop exception handler installed, 20 runs, synchronous assertion with no
`gc.collect()`):

```
[CURRENT (with fix)]                      closed_at_disconnect: 20/20 | loop_errors: 0 | released==1 every run: True
[PRE-FIX sim: plain StreamingResponse]    closed_at_disconnect:  0/20 | loop_errors: 0
[PRE-FIX sim: nested client.stream]       closed_at_disconnect:  0/20 | (RuntimeError "generator didn't stop after athrow()" on stderr, ~10x)
```

- **Released at disconnect time, not GC time:** 20/20 with the fix vs **0/20** with either
  change reverted — attributable to the fix, not refcount timing.
- **`RuntimeError` gone:** 0 loop errors in 20/20; 0 `sys.unraisablehook` events.
- **No new leak / no double-close:** a close counter on the upstream stream shows `closes == 1`
  on every path — full drain, early `aclose()`, upstream-raises, server cancellation, and pure
  GC-abandon.
- **`_ReleasingStreamingResponse` correctness:** `isinstance(async_gen, AsyncGenerator)` is
  `True`, so the guard is not dead code. Closing an already-drained iterator is a no-op (verified
  by double-`aclose()`, no raise) and the full-drain path yields `closes=1`, not 2.

**F3 — FIXED.** `test_non_json_upstream_error_body_is_wrapped` asserts concrete values (502
propagated, `content-type: application/json`, `type=upstream_error`, `param/code is None`,
`HTTP 502` and `<html>Bad Gateway</html>` in the message) and discriminates: without
`_non_json_body_error`, `response.json()` raises inside `_forward_buffered` → 500.
`test_forwardable_response_headers_drop_framing_headers` asserts the exact surviving set
`{"x-request-id":"req-1","x-ratelimit-remaining":"17"}` with the five dropped headers absent.

**F4 — FIXED.** `test_request_header_sanitisation_is_case_insensitive_and_honours_connection`
calls `_build_upstream_headers` directly with mixed-case keys, bypassing Starlette's
lower-casing. Asserts `authorization_keys == ["Authorization"]` (exactly one), value
`Bearer <provider.api_key>`, and the inbound token absent. A `.lower()`-less implementation
would yield two authorization keys and fail.

**F5 — FIXED.** `_connection_nominated_headers` (`proxy.py:81-98`) parses the comma-separated
`Connection` field and unions it into the drop set (`proxy.py:117`). The F4 test sends
`Connection: keep-alive, X-Internal-Trace` and asserts `x-internal-trace` is not forwarded.

**F2 — ACCEPTED AS LIMITATION (honestly documented).** Reproduced: upstream
`500 {"error":"boom"}` on a `stream=True` request → caller sees **200** `text/event-stream` with
the raw error body. Documented in handoff *Known limitations* item 4 with cause and required
future change.

**F6 — ACCEPTED AS LIMITATION (honestly documented).** Reproduced: an upstream `x-request-id` is
**not** relayed on `stream=True` (the buffered path relays it: `429` → `x-request-id: req-7`).
Documented in handoff item 5.

## Spec compliance

### §Scope 1 — `src/gateway/engine/proxy.py`

| Requirement | Result | Evidence |
|---|---|---|
| Exact `forward_request(client, target, provider, request_data, stream, headers)` signature | **MET** | `proxy.py:223-230`; `inspect.signature` → exactly those 6 params; 2 overloads (`Literal[False]`/`Literal[True]`) |
| Header sanitation: strip hop-by-hop, set `Authorization: Bearer <api_key>` | **MET** | `proxy.py:37-70`, `:101-122`; test 3 + F4 test |
| Non-streaming returns `(status_code, json, headers)` | **MET** | `proxy.py:157-170` |
| Streaming yields `response.aiter_bytes()` with clean closure on termination | **MET** | `proxy.py:173-198`. Mechanism is `client.send(request, stream=True)` + `try/finally: await response.aclose()` instead of the nested `async with client.stream(...)`; the spec's intent (raw chunks, clean close on termination) is satisfied and the change was required to close F1 — semantically equivalent to `client.stream`'s own `send(..., stream=True)` + `finally: aclose()` |
| Zero-copy / no buffering | **MET** | Own gated probe: first chunk returned while upstream log was `['c1']` (chunk 2 withheld) |
| Context safety: no leaked socket on disconnect | **MET** | 20/20 harness; all four exit paths + GC path `closes==1` |

### §Scope 2 — `src/gateway/main.py`

| Requirement | Result | Evidence |
|---|---|---|
| Lifespan: `load_config()`, shared client with 50/200 limits on `app.state` | **MET** | `main.py:133-150`; `create_app(None)` defers to `load_config()` |
| Shutdown awaits `aclose()` | **MET** | `main.py:149-150` |
| `GET /health` | **MET** | `main.py:158-161`; observed 200 |
| `GET /v1/models` | **MET** | `main.py:163-170`, `:95-116`; observed `['gpt-6.1-sol','gpt-6-astra']` |
| `POST /v1/chat/completions` | **MET** | `main.py:172-214` |
| `run_cli()` via `uvicorn.run` honoring host/port | **MET** | `main.py:224-234`; `__main__` guard at `:237-238` |

### §Scope 3 — `tests/test_proxy.py`
All five required tests present and passing, plus 4 added tests. In-process via `respx` +
`httpx.ASGITransport`; **no real ports bound**.

### §Constraints

| Constraint | Result | Evidence |
|---|---|---|
| Zero-copy streaming | **MET** | gated probe |
| No unclosed socket on disconnect | **MET** | 20/20 harness |
| Strict types, no `TODO` | **MET** | full annotations, real overload pair, `JSONValue`; regex scan → no markers |
| Full suite green | **MET** | `12 passed` (3 config + 9 proxy) |

## Findings

No BLOCKER and no MAJOR findings.

1. **NIT** — `main.py:68` — the guard `isinstance(body_iterator, AsyncGenerator)` would silently
   leave a *non*-async-generator `AsyncIterable` unclosed. Correct today (the only producer is the
   async generator from `forward_request`, and `isinstance` verified `True`); robustness note only.
   "Fixed" = `hasattr(body_iterator, "aclose")`. No action required.
2. **NIT** — `main.py:63-69` — if `super().__call__` raised and `body_iterator.aclose()` also
   raised, the original exception would be masked. Practically unreachable (verified), defensive
   note only.
3. **MINOR (ACCEPTED LIMITATION — no action this round)** — `main.py:207` / `proxy.py:173-198` —
   F2 (streaming always returns 200) and F6 (streaming drops upstream response headers) remain real
   transparency gaps, reproduced above. Both are structurally forced by the spec-pinned iterator-only
   streaming signature and are documented in the handoff with the required future change.

## Verified independently

- **Full suite** (project root, `-p no:cacheprovider`): `12 passed in 3.13s` (3 config + 9 proxy).
- **Disconnect harness** (20 runs, own code): `closed_at_disconnect: 20/20 | loop_errors: 0`.
  Discriminating variants: plain `StreamingResponse` → `0/20`; nested `client.stream` → `0/20`
  **and** `RuntimeError("generator didn't stop after athrow()")` on stderr.
- **Four exit paths + GC** (close-counter probe): full drain `closes=1`; early `aclose()`
  (double-`aclose` safe) `closes=1`; upstream raises `propagated='RuntimeError: upstream died
  mid-stream' closes=1`; server cancellation `closes=1`; GC-abandon `closes=1 unraisable=[]`.
- **Full drain through the real ASGI app:** `status=200 body=b'abc' upstream_closed=True closes=1`.
- **No buffering** (own gated upstream): `first within 2s` while `upstream_log=['c1']`; then
  `second` with `['c1','c2']`; after `aclose` `['c1','c2','aclose']`.
- **Signature/overloads:** 6 params exactly, `overloads: 2`.
- **Import inertness:** `app type: FastAPI`, `http_client before lifespan: False`, all three
  endpoints present, `run_cli callable: True`, `__main__` guard present.
- **No real ports:** audit hook over `tests/test_proxy.py` → 7 binds, all `('127.0.0.1', 0)`.
- **Task 1.1 unchanged:** SHA-256 prefixes match the Builder's claim exactly —
  `cee462a0a5b5fdc6 config.py (9250)`, `bcad860f783d924d __init__.py (526)`,
  `65e12e3884a7bf25 config.example.yaml (1442)`, `385809c9b63c99a7 pyproject.toml (717)`,
  `fd203d6c652d9c52 tests/test_config.py (4859)`. No git repo exists, so this is the strongest
  available baseline; the 3 config tests also still pass.
- **Original 5 proxy tests intact:** all five names present with substantive assertions — none
  deleted or weakened.

## Verdict rationale

F1 is genuinely and deterministically fixed — the independent 20-run harness shows 20/20
release-at-disconnect versus 0/20 for both reverted variants, the intermittent `RuntimeError` is
gone on every path including pure GC finalization, and no new leak or double-close was introduced
(every path closes exactly once). F3, F4 and F5 are fixed with concrete, discriminating tests;
F2 and F6 remain present but are honestly documented as out-of-scope limitations. Spec compliance
is complete (exact signature preserved, lifespan/pooled client, all three endpoints, `run_cli`,
zero-copy streaming, no real ports), Task 1.1 is untouched, and the only remaining items are two
NITs and two accepted limitations — no BLOCKER, no MAJOR ⇒ **APPROVED**.
