# Review — Task 1.3 (Round 1)

STATUS: APPROVED

Reviewer: independent read-only review (fresh agent instance, no prior context).
Note: the configured Reviewer model `Atria-Dawn-Preview` was unavailable (429 token quota
exhausted), so this round ran on the `hy4-preview-f` model with the Reviewer agent's read-only
tool restrictions intact. All evidence below was produced by the Reviewer's own probes; neither
the Builder's handoff nor the orchestrator's probe was relied upon.

## Spec compliance

### §Scope 1 — `src/gateway/engine/router.py`

| Requirement | Result | Evidence |
|---|---|---|
| Iterate `route.targets` in configured priority order | **MET** | `_select_targets` order-preserving (`router.py:123-125`); loop at `:213` |
| Retryable set **exactly** `[429, 500, 502, 503, 504]` | **MET** | `RETRYABLE_STATUS_CODES` (`router.py:48`); probe printed `[429, 500, 502, 503, 504]` |
| Retryable exceptions `httpx.TimeoutException`, `httpx.NetworkError` | **MET** | `except (httpx.TimeoutException, httpx.NetworkError) as exc:` (`router.py:229`) — specific pair, no bare `except` |
| Cooldown on 429/5xx with ~30 s TTL | **MET** | `DEFAULT_COOLDOWN_SECONDS = 30.0` (`:51`); `record_failure` at `:236`, `:243`; `_expiries[key] = clock() + ttl` (`:96`) |
| Skip cooling targets unless **all** are cooling | **MET** | `_select_targets` (`:114-125`): `available or targets` |

### §Scope 2 — `src/gateway/engine/proxy.py`

| Requirement | Result | Evidence |
|---|---|---|
| Streaming carries status + headers before the client stream commits | **MET** | `StreamingResult` 4-tuple (`proxy.py:49-51`); `_forward_streaming` (`:239-284`) |
| `status >= 400` → consume error body, return `(status, error_json, headers, None)` | **MET** | `:268-271`; `_read_error_body` `aread()`+`aclose()` in `finally` (`:196-212`). Probe: `429, iter=None, closes=1` |
| `200` → buffered first chunk + chained remainder, `(200, None, headers, gen)` | **MET** | `:273-284`; `_chained_stream` (`:215-236`). Probe: exactly one chunk read ahead |
| `_ReleasingStreamingResponse` wraps the chained generator | **MET** | `main.py:220-225`; class at `:55-75` |

### §Scope 3 — `src/gateway/main.py`

| Requirement | Result | Evidence |
|---|---|---|
| Route through the failover engine (not `targets[0]`) | **MET** | `dispatch(...)` at `main.py:210-212`, `:227-229`; no `targets[0]` in `main.py` |
| Log a warning per failover | **MET** | `_log_failover` (`router.py:139-152`) at WARNING; observed on stderr |
| Streaming success returns the **real upstream status + headers** | **MET** | `main.py:220-225`; probe: `200`, `x-request-id: rid-9`, `x-ratelimit-remaining: 42` relayed; non-retryable `400` surfaced as `400` |
| All fail → **final** target's status + `gateway_failover_exhausted` envelope | **MET** | `router.py:253-261`; `main.py:213-219`. Test 4 asserts 429, Test 4b asserts 504 — final status, not a hard-coded 502 |

### §Scope 4 — `tests/test_failover.py`
All six required tests present and substantive: buffered 429→200 (`:145`), streaming 502→SSE
byte-exact (`:191`), timeout→200 (`:243`, plus streaming variant `:265`), all-429 envelope
(`:300`, plus streaming variant `:334`), cooldown skip (`:362`), disconnect releases every stream
(`:506`). 13 tests total.

### §Constraints

| Constraint | Result | Evidence |
|---|---|---|
| Backward compat with 1.1/1.2 tests | **MET** | `25 passed`; `tests/test_config.py` SHA-256 `fd203d6c652d9c52`, 4859 bytes — matches byte-for-byte |
| Zero socket leaks on disconnect | **MET** | Own harness, 20 runs × 2 paths: `closed_at_disconnect 20/20`, `closes dist [1]` |
| Deterministic, zero-network-bound suite | **MET** | Audit hook: one bind `('127.0.0.1', 0)`, `getaddrinfo calls: 0`; no sleeps |
| Fully typed (mypy compliant) | **PARTIAL (unverifiable)** | `mypy` not installed. Overload pairs verified at runtime instead: `get_overloads` == 2 for both `forward_request` and `dispatch`. Disclosed in handoff. |

## Findings

**1 — MINOR — `router.py:65,149` / `proxy.py:337-348` — `target.model` is never used to rewrite the outgoing `model` field.**
`target.model` appears only in `_target_key` (cooldown identity) and the log message; the payload is
forwarded verbatim. Consequence: the backup target advertises `gpt-6-astra`, but a failover relays
the caller's alias `gpt-6.1-sol`, which the backup does not advertise — so a "successful" failover
may return an upstream 404/400.

*The Reviewer justified this as forced by §Constraints + Task 1.2's transparency assertion. The
orchestrator checked that claim and it is **incorrect**: `test_non_streaming_chat_completion_is_transparent`
uses payload model `gpt-6.1-sol`, which equals `coding_route.targets[0].model`, so a rewrite would be
a no-op and the assertion would still pass. This is therefore an unmade design decision, not a forced
one. Escalated to the user rather than actioned.*

**2 — NIT — `router.py:240-245` — the failover warning is emitted even for the last target, where no failover occurs.**
`_log_failover(target, f"HTTP {status_code}", remaining)` runs with `remaining == 0` for the final
target, so the log reads `… failing over. 0 target(s) remaining.` followed by `All N upstream
target(s) … failed`. Misleading wording only. **Fixed in round 2.**

**3 — NIT — `proxy.py:279` — `except BaseException:` with `await response.aclose(); raise`.**
Not a bare `except` and not `except Exception`: it deliberately catches `CancelledError` so a
half-open upstream is closed on cancellation, and it re-raises. Correct and intentional.

**4 — NIT — `main.py:224` — streaming success forces `media_type="text/event-stream"`.**
`_forwardable_response_headers` strips `content-type`, so a streaming upstream returning a different
type is relabelled. Correct for a chat-completions SSE proxy; disclosed in the handoff.

**5 — NIT — `tests/test_proxy.py:447-481` — hunk 3 changes the mocked status 502 → 400.** Adjudicated
as forced (see below).

No BLOCKER and no MAJOR findings.

## Adjudications

**Hunk 3 — forced, not test weakening. CONFIRMED.** The unmodified test mocks only the primary with a
502 and leaves the backup unmocked, then asserts the caller receives `502`. Any implementation that
fails over on 502 (spec §Scope 1) necessarily attempts the backup, which respx rejects with
`AllMockedAssertionError` → the caller gets 500, not 502. Escape hatches enumerated and all fail:
(a) not failing over on 502 violates the spec; (b) returning the *first* target's status on exhaustion
still routes through the unmocked backup; (c) the two targets are on different hosts
(`api.sharellm.example.com` vs `openrouter.ai`), so the backup URL is never the mocked one. The
adapted test still verifies its original subject (non-JSON body wrapped into the `upstream_error`
envelope, still discriminating) — only the status number and matched HTML string changed. Coverage is
relocated, not lost.

**F2 and F6 — both genuinely RESOLVED. CONFIRMED.** F6: `x-request-id` and `x-ratelimit-remaining`
relayed on streaming success. F2: upstream `400` on a `stream=True` request reaches the caller as
`400` with the upstream JSON body, not `200`.

**`target.model` — see Finding 1.** The Reviewer agreed with forwarding verbatim *for this task* but
flagged the underlying gap; the orchestrator additionally refuted the "forced" justification.

## Verified independently

- **Full suite:** `25 passed in 3.49s` — `test_config 3 / test_proxy 9 / test_failover 13`.
- **Isolation / ordering:** `test_failover.py` alone → 13 passed; `test_failover + test_proxy` → 22
  passed; full → 25 passed. Same result every order. No `conftest.py`.
- **Task 1.1 byte-identity:** `fd203d6c652d9c52… 4859 tests/test_config.py`; also `cee462a0… config.py`,
  `bcad860f… __init__.py`, `65e12e38… config.example.yaml`, `385809c9… pyproject.toml` — all match baseline.
- **Own disconnect harness (20 runs × 2 paths, real ASGI app, `send` blocked on the buffered first
  chunk's `yield`, synchronous assertions, no `gc.collect()`):**
  ```
  failover=False: closed_at_disconnect 20/20 | released==[first] 20/20 | loop_errors=0
                  backup.closes dist [1] | primary.closes dist [0]
  failover=True:  closed_at_disconnect 20/20 | released==[first] 20/20 | loop_errors=0
                  backup.closes dist [1] | primary.closes dist [1]
  ```
- **TTFT `>= 400` path:** `429 iter=None closes=1 released=[b'{"error": "rate limited"}']`.
- **Non-retryable does not consume targets:** engine `stream=True` 400 → `primary 1, backup 0`;
  buffered 404 → `primary 1, backup 0`.
- **Zero-copy / exactly one chunk ahead (gated upstream):** first chunk delivered while `log ==
  ['produced 1']` and the gate unset; nothing beyond the single first chunk buffered.
- **Extra leak paths probed:** first-chunk `ReadTimeout` → `closes = 1` then re-raised and fails over
  to the backup; upstream dies mid-stream after the first chunk → `closes 1`.
- **Cooldown clock genuinely injected:** fixed fake clock at `t=0` → `is_cooling True`; at `t=30` →
  `False`. Default clock is `time.monotonic`.
- **Hygiene:** grep over `src/gateway` for `TODO|FIXME|XXX|HACK|placeholder|stub|mock|print(|except
  Exception|except :` → no matches. No sleeps in `tests/`.
- **Import inertness:** `http_client before lifespan: False`, `cooldown_registry before lifespan:
  False`, no sockets bound during import.
- **No real ports:** single bind `('127.0.0.1', 0)`, `getaddrinfo calls: 0`.
- **9 proxy tests intact:** hunks 1–2 match the handoff's AFTER exactly and *add* assertions
  (`status_code == 200`, `error_body is None`, `iterator is not None`) — strengthening, not weakening.

## Verdict rationale

The failover engine matches the spec item-by-item: ordered targets, the exact retryable set, the exact
retryable exception pair, a 30 s injectable-clock cooldown with the "all-cooling ⇒ full order"
fallback, the 4-tuple streaming contract with TTFT inspection, a chained generator whose `try/finally`
covers the buffered first chunk's `yield`, `/v1/chat/completions` routed through `dispatch` with a
per-failover warning and the final target's status on exhaustion, and all six required tests. The
independent 40-run disconnect harness reproduces release-at-disconnect 20/20 on both paths with
exactly one close per stream and zero loop errors, and F2/F6 are demonstrably resolved. Task 1.1 is
byte-identical and the suite is order-independent and port-free. The only `tests/test_proxy.py`
deviation (hunk 3) is provably forced by the spec's own retryable set. Remaining items are
MINOR/NIT design notes — no BLOCKER, no MAJOR ⇒ **APPROVED**.
