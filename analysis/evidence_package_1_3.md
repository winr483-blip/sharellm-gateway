# Evidence Package — Task 1.3 (Review Round 1)

## 1. Task specification

`analysis/task-1.3.md` — "Task 1.3 — First-Chunk Inspection & Resilient Failover Engine".
Read it in full, including the section **"Resolution of the contract conflict"**.

Summary: add `src/gateway/engine/router.py` (ordered target selection, retryable
`[429,500,502,503,504]` + `(httpx.TimeoutException, httpx.NetworkError)`, soft cooldown circuit
breaker); refactor `forward_request` so streaming carries upstream status + headers and does
TTFT first-chunk inspection; wire `/v1/chat/completions` through the failover engine; add
`tests/test_failover.py` with six required tests.

## 2. Project root

    C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway

## 3. Builder handoff

`analysis/builder_handoff_1_3.md` — STATUS: COMPLETE. It contains the required explicit
before/after diff of every `tests/test_proxy.py` change.

## 4. Files created / changed

    src/gateway/engine/router.py     (created)
    tests/test_failover.py           (created)
    src/gateway/engine/proxy.py      (modified)
    src/gateway/main.py              (modified)
    tests/test_proxy.py              (modified — 3 hunks, documented)

Unchanged: `tests/test_config.py`, `src/gateway/config.py`, `src/gateway/__init__.py`,
`config.example.yaml`, `pyproject.toml`. No git repository exists — inspect files directly.

## 5. Reviewer reading list (priority order)

1. `src/gateway/engine/router.py`
2. `src/gateway/engine/proxy.py`
3. `src/gateway/main.py`
4. `tests/test_failover.py`
5. `analysis/builder_handoff_1_3.md` (esp. the `tests/test_proxy.py` diff section)
6. `tests/test_proxy.py`
7. `config.example.yaml`, `analysis/task-1.3.md`

## 6. Build / test output

    cd "C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway" && \
      C:/Users/Notebook/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe -m pytest -q

    25 passed in 3.59s

Composition: 3 config + 9 proxy + 13 failover. `tests/test_config.py` is byte-identical to its
Task 1.1 state (SHA-256 prefix `fd203d6c652d9c52`, 4859 bytes) — independently re-checked by the
orchestrator.

## 7. Orchestrator's independent probe (outside the Builder's test files)

Observed output:

    [1 buffered 429->200] 200 {'ok': 'backup'} | t1=1 t2=1
    [2 stream 502->SSE] 200 ct=text/event-stream; charset=utf-8 xrid=rid-9 exact=True | t1=1 t2=1
    [3 timeout->200] 200 {'ok': 'after-timeout'} | t1=1 t2=1
    [4 all-429] 429 {"error": {"message": "All upstream targets failed", "type": "gateway_failover_exhausted"}}
    [5 cooldown] after req1 t1=1 t2=1 | after req2 t1=1 t2=2 (t1 must stay 1)
    [6 non-retryable 400] 400 {'error': {'message': 'bad model'}} | t1=1 t2=0 (t2 must be 0)
    [7 F2 stream real status] 400 ct=application/json body={'error': {'message': 'nope'}}
    [8 disconnect during failover] t2 stream closed at disconnect: True | loop_errors=0 | released=1

WARNING-level failover logs were emitted as expected, e.g.
`Upstream target 'sharellm_primary' (model 'gpt-6.1-sol') failed (HTTP 429); failing over. 1 target(s) remaining.`

## 8. Specific claims to adjudicate

1. **The `tests/test_proxy.py` adaptation.** Three hunks are documented with before/after.
   Hunks 1–2 are mechanical 4-tuple unpacks at the two engine-level streaming call sites.
   Hunk 3 changes `test_non_json_upstream_error_body_is_wrapped` from a mocked **502** to a
   mocked **400**, because 502 is now retryable and would be swallowed by failover before the
   body could reach the caller. **Judge whether hunk 3 is genuinely forced or is test weakening.**
   The orchestrator's view: forced — there is no implementation that both fails over on 502 as
   the spec requires *and* lets that test pass unmodified.

2. **Task 1.2's F2/F6 are claimed RESOLVED.** F6: streaming now relays upstream headers
   (`x-request-id` observed). F2: a streaming request surfaces the real upstream status rather
   than always 200. Verify both independently.

3. **Zero-leak guarantee preserved through the new chained generator.** The TTFT path buffers a
   first chunk, then chains the rest. Verify the upstream response is still released **at
   disconnect time** (not by the GC finalizer) — including when the consumer abandons the stream
   while parked on the *buffered first chunk's* `yield`. Also verify the `status >= 400` TTFT
   path closes the response after reading the error body.

4. **Cooldown state must not leak across tests.** The registry is instance-scoped and created per
   app in the lifespan. Verify the suite is order-independent (run the full suite and
   `tests/test_failover.py` alone; also try a reversed order if convenient).

5. **Non-retryable statuses must not consume the remaining targets** (probe #6 above shows
   target 2 was never called for a 400). Verify independently.

6. **`target.model` is NOT used to rewrite the outgoing `model` field** — the payload is forwarded
   verbatim, preserving Task 1.2's approved transparency contract. The 1.3 spec is silent on this.
   Judge whether that is the right call.

## 9. Reviewer instruction

Verify against `analysis/task-1.3.md` yourself. Do not take the Builder's claims or the
orchestrator's probe on trust — re-read the files and re-run what you need. Write your own
probes. Report a structured verdict.
