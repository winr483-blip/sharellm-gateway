# Review — Task 1.3 (Round 2)

STATUS: APPROVED

Reviewer: independent read-only review (fresh agent instance, no prior context).
Note: the configured Reviewer model `Atria-Dawn-Preview` was unavailable (429 token quota
exhausted), so this round ran on the `hy4-preview-f` model with the Reviewer agent's read-only
tool restrictions intact. All evidence was produced by the Reviewer's own probes.

## Round-1 delta adjudication

**Delta 1 — logging fix in `src/gateway/engine/router.py` → FIXED.**
`_log_failover` (`router.py:139-168`) now branches on `remaining` at `:152`: the "failing over to
the next target. N target(s) remaining." wording is emitted only when a further target exists, and a
distinct `… no target remains (last target in the route).` otherwise (`:162-168`). Both call sites
route through it — the retryable-**status** path (`:260`) and the retryable-**exception** path
(`:253`) — so the two failure kinds are consistent. `remaining` is computed as
`len(targets) - index - 1` (`:231`), correct: 0 only on the last selected target.

Real `LogRecord`s captured by the Reviewer (handler on `gateway.engine.router`, real ASGI app):

- Non-last target fails (status): `[WARNING] Upstream target 'sharellm_primary' (model 'gpt-6.1-sol') failed (HTTP 429); failing over to the next target. 1 target(s) remaining.`
- Last target fails on a status: `[WARNING] Upstream target 'openrouter_backup' (model 'gpt-6-astra') failed (HTTP 429); no target remains (last target in the route).`
- Last target fails on a timeout: `[WARNING] Upstream target 'openrouter_backup' (model 'gpt-6-astra') failed (ReadTimeout: boom); no target remains (last target in the route).`

The `All N upstream target(s) for route failed; returning HTTP 429./504.` summary still fires
(`:269-273`). Every record is at WARNING. The old misleading `failing over. 0 target(s) remaining.`
line is gone.

**Delta 2 — documentation correction in `analysis/builder_handoff_1_3.md` → FIXED (doc-only).**
*Known limitations* #2 (`builder_handoff_1_3.md:445-455`) now reads "an *unmade design decision*, not
a forced one", states "It is **not** forced by backward compatibility", and explains the
`test_non_streaming_chat_completion_is_transparent` no-op argument. The `## FIXES APPLIED (round 1)`
§(a) (`:503-518`) withdraws the earlier claim. No residual assertion that verbatim forwarding was
compelled. No code change accompanies it.

**No third change.** Both deltas are consistent with the current tree; the suite, failover outcomes
and leak harness are identical to pre-fix values.

## Spec compliance

| Requirement | Result | Evidence |
|---|---|---|
| §1 ordered `route.targets` iteration | MET | `_select_targets` (`router.py:123-125`); loop `:229` |
| §1 retryable set **exactly** `[429,500,502,503,504]` | MET | `router.py:48`; probe printed `[429, 500, 502, 503, 504]` |
| §1 retryable exceptions pair | MET | `except (httpx.TimeoutException, httpx.NetworkError)` (`router.py:245`) |
| §1 cooldown TTL 30 s; skip cooling unless **all** cooling | MET | `:51`, `:94-107`, `available or targets` (`:124-125`). Cooled-t0 → `['openrouter_backup']`; both cooled → full order; `t+30` → expired |
| §2 streaming carries status+headers pre-commit | MET | `StreamingResult` 4-tuple (`proxy.py:49-51`); `:239-284` |
| §2 `status>=400` → consume body, return `(status,error_json,headers,None)` | MET | `proxy.py:268-271`; `_read_error_body` (`:196-212`) |
| §2 `200` → buffered first chunk + chained remainder | MET | `proxy.py:273-284`; `_chained_stream` (`:215-236`) |
| §2 `_ReleasingStreamingResponse` wraps the generator | MET | `main.py:55-75`, used `:220-225` |
| §3 route through `dispatch`, not `targets[0]` | MET | `main.py:210-212`, `:227-229`; grep `targets[0]` in `src/gateway` → no matches |
| §3 warning per failover | MET | `router.py:253`, `:260`; observed at WARNING |
| §3 streaming success relays **real** status + headers | MET | `main.py:220-225`; probe: `x-request-id: rid-9`, `x-ratelimit-remaining: 42`; non-retryable `400` surfaced as `400` |
| §3 all-fail → **final** target's status + envelope | MET | `router.py:256-261`, `:269-277`; probes returned `429` and `504` |
| §4 six required tests | MET | 13 tests total in `test_failover.py` |
| §Constraints backward compat | MET | `25 passed`; `tests/test_config.py` SHA-256 `fd203d6c652d9c52…`, 4859 bytes |
| §Constraints zero socket leaks | MET | Own harness, 20 runs × 2 paths: `closed_at_disconnect 20/20`, `closes == [1]`, `loop_errors 0` |
| §Constraints deterministic, zero-network-bound | MET | Audit hook: binds `['127.0.0.1']:[0]`, `getaddrinfo calls: 0`; no sleeps |
| §Constraints fully typed (mypy) | PARTIAL (unverifiable) | `mypy` not installed; overload pairs verified (`get_overloads` == 2). Disclosed |

## Findings

No BLOCKER and no MAJOR findings. The Round-2 delta is correct and regression-free.

1. **MINOR (carried over, escalated) — `router.py:65` / `proxy.py:337-348` — `target.model` never
   used to rewrite the outgoing `model` field.** A failover relays the caller's alias
   (`gpt-6.1-sol`) to the backup, which advertises `gpt-6-astra`. This is the unmade design decision
   escalated to the user; the handoff correction now states it accurately. Not a regression.
2. **NIT (carried over) — `main.py:224` — streaming success forces `media_type="text/event-stream"`.**
   `_forwardable_response_headers` strips `content-type`. Correct for an SSE chat-completions proxy.
3. **NIT (carried over) — `router.py:258` / `main.py:218` — the all-fail envelope relays the final
   target's forwardable headers** (e.g. `retry-after`, `x-ratelimit-*`) alongside a gateway-generated
   body. Spec silent; `content-type` stripped so framing stays consistent.

## Verified independently

- **Full suite:** `25 passed in 3.44s` (3 config + 9 proxy + 13 failover).
- **Byte-identity (own SHA-256):** `fd203d6c652d9c52… 4859 tests/test_config.py`; `cee462a0… config.py`;
  `bcad860f… __init__.py`; `65e12e38… config.example.yaml`; `385809c9… pyproject.toml` — all unmodified.
- **Failover outcomes (own ASGI probes, fresh app per case):**
  ```
  (1) buffered 429->200: status=200 body_ok=True p=1 b=1
  (2) streaming 502->SSE: status=200 ct=text/event-stream byte_exact=True p=1 b=1
      primary_closed=True backup_closed=True primary_released=[b'{"error":"bad gateway"}']
  (3) timeout->200: status=200 body_ok=True p=1 b=1
  (4) all-429: status=429 body={'error': {'message': 'All upstream targets failed', 'type': 'gateway_failover_exhausted'}}
  (5) cooldown: r1=200 r2=200 primary_calls=1 backup_calls=2
  (6a) F6 stream success: status=200 ct=text/event-stream xrid=rid-9 xrl=42
  (7) F2 stream real status: 400 ct=application/json body={'error': {'message': 'nope'}}
  ```
- **Own disconnect harness (20 runs × 2 paths, `send` blocked on the buffered first chunk's `yield`,
  synchronous assertions, no `gc.collect()`):**
  ```
  failover=False: closed_at_disconnect 20/20 | released==[first] 20/20 | backup.closes [1] | primary.closes [0]
  failover=True:  closed_at_disconnect 20/20 | released==[first] 20/20 | backup.closes [1] | primary.closes [1]
  loop_errors: 0 []
  ```
- **Cooldown semantics (direct):** TTL `30.0`; cooled-t0 → `['openrouter_backup']`; both cooled →
  full order; `t+30` boundary → `is_cooling(t0) False`.
- **Import inertness / ports:** `has http_client before lifespan: False`,
  `has cooldown_registry before lifespan: False`, no binds/DNS; whole suite under an audit hook →
  binds `['127.0.0.1']:[0]`, `any non-zero port bound: False`, `getaddrinfo calls: 0`.
- **Hygiene:** grep over `src/gateway` for `targets[0]|TODO|FIXME|XXX|HACK|placeholder|stub|print(|except
  Exception|except:|sleep` → no matches; `run_cli()` guarded (`main.py:255-256`); no sleeps in `tests/`.
- **`test_proxy.py` hunks:** the three documented hunks match the current file exactly
  (hunk 1 `:272-282`, hunk 2 `:335-344`, hunk 3 `:447-481`). No further edits.

## Verdict rationale

The Round-2 delta is exactly what was claimed and nothing more: `_log_failover` emits the "failing
over" wording only when a target actually remains, with a distinct accurate "no target remains"
warning for the final attempt, on both the status and exception paths, all at WARNING with the
exhaustion summary intact — reproduced from real `LogRecord`s. The documentation correction withdraws
the "forced by backward compatibility" claim cleanly. Every failover outcome, the byte-exact SSE relay
with the failed primary closed, F2/F6, the cooldown skip, the 20/20-at-disconnect leak harness, the
byte-identical Task 1.1 artefacts and the port-free audit are unchanged from the pre-fix state, so no
regression was introduced. Remaining items are the same carried-over MINOR/NIT design notes — no
BLOCKER, no MAJOR ⇒ **APPROVED**.
