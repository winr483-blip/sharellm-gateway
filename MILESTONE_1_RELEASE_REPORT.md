# ShareLLM Smart Gateway — Milestone 1 Release Report

**Release:** Milestone 1 (Sprint 1) — MVP
**Version:** 0.1.0
**Date:** 2026-10-14
**Status:** FROZEN — approved by independent review (2 rounds, Task 1.5; 2 rounds each for Tasks 1.1–1.4)

---

## 1. Summary

Milestone 1 delivers a working, fully typed, multi-provider LLM routing gateway: a transparent
OpenAI-compatible reverse proxy with resilient failover, first-chunk (TTFT) stream inspection, a soft
circuit breaker, ShareLLM error normalization, prompt-cache telemetry and an anti-422 request guard.

The frozen artifact is **81 tests green**, **`mypy --strict` clean across 11 source files**, and
verified to leak **zero sockets** on client disconnect.

| Gate | Result |
|---|---|
| `pytest` | **81 passed** (0 skipped, 0 xfailed) |
| `mypy --strict src/gateway` | **Success: no issues found in 11 source files** |
| Socket leaks on disconnect | **0** — 20/20 runs released at disconnect time, exactly one close per stream |
| Real network ports bound by tests | **0** (`socket.listen: 0`; all binds are the loopback ephemeral self-pipe) |
| Source + test size | 20 files, 4,797 lines |

---

## 2. Scope delivered

| Task | Deliverable | Review |
|---|---|---|
| **1.1** | Repository scaffold, `.gitignore`, `pyproject.toml`, `config.example.yaml`, typed config engine (Pydantic v2 + PyYAML) with fail-fast provider-reference validation | APPROVED (round 2) |
| **1.2** | Core transparent reverse proxy: `forward_request`, header sanitation, SSE streaming pass-through, FastAPI app with lifespan-managed pooled `httpx.AsyncClient`, `/health`, `/v1/models`, `/v1/chat/completions` | APPROVED (round 2) |
| **1.3** | Resilient failover engine: ordered target selection, retryable status/exception policy, `CooldownRegistry` soft circuit breaker, TTFT first-chunk inspection, streaming status/header relay | APPROVED (round 2) |
| **1.4** | Target model rewriting, ShareLLM error sanitization, cache telemetry, anti-422 parameter sanitizer | APPROVED (round 2) |
| **1.5** | E2E chaos journey, CLI banner + `python -m gateway` entry point, word-boundary rate-limit fix, Milestone freeze | APPROVED (round 2) |

---

## 3. Architecture

```
src/gateway/
├── __init__.py            public API surface (__version__ = "0.1.0")
├── __main__.py            `python -m gateway` entry point
├── config.py              Pydantic v2 typed config engine + load_config()
├── main.py                FastAPI app, lifespan, endpoints, CLI (banner + --config)
├── engine/
│   ├── proxy.py           forward_request: header sanitation, buffered + TTFT streaming
│   └── router.py          dispatch(): ordered failover, retryable policy, CooldownRegistry
├── adapters/
│   ├── sharellm.py        sanitize_error_envelope, extract_cache_stats, cache logging
│   └── sanitizer.py       sanitize_request_payload (anti-422 guard)
└── utils/                 (reserved — empty)
```

**Request lifecycle**

```
client → /v1/chat/completions
      → sanitize_request_payload        (strip 422-triggering empty/null tool fields)
      → dispatch()                      (per-target: rewrite model → forward → judge)
          → forward_request()           (sanitize headers → upstream)
              · buffered: await JSON
              · streaming: TTFT — inspect status, buffer ONE chunk, chain the rest
          → retryable? 429/500/502/503/504 or Timeout/NetworkError → cooldown + next target
          → otherwise relay verbatim (status + headers + body)
      → sanitize_error_envelope         (share_llm_error → OpenAI error types)
      → cache telemetry                 (non-streaming success only)
```

**Two design decisions worth knowing**

1. **Provider-reference validation is deliberately two-layered** (`config.py`): an `AppConfig`
   `model_validator` guarantees an un-constructible config, plus a pre-construction pass in
   `load_config` so the loader raises a genuine **plain** `ValueError` rather than a Pydantic
   `ValidationError` subclass.
2. **Streaming is zero-copy and TTFT-inspected.** Exactly one chunk is read ahead to prove the stream
   is viable before the client's stream commits; the upstream response is released at **disconnect
   time** via an explicit `client.send(..., stream=True)` + `try/finally: aclose()` (not via the
   async-generator GC finalizer) and a `_ReleasingStreamingResponse` that owns the iterator.

---

## 4. Verification evidence (all re-run at freeze time)

```
$ python -m pytest -q
81 passed in 5.33s
  tests/test_adapters.py    36
  tests/test_config.py       3
  tests/test_e2e_chaos.py   20
  tests/test_failover.py    13
  tests/test_proxy.py        9

$ python -m mypy --strict src/gateway
Success: no issues found in 11 source files

$ python -m gateway --help
usage: gateway [-h] [--config PATH]
$ sharellm-gateway --help
usage: sharellm-gateway [-h] [--config PATH]

Socket audit over the whole suite: TOTAL_BINDS 27, LISTENS 0,
DISTINCT_BINDS ["('127.0.0.1', 0)"], NON_EPHEMERAL_BINDS []
Disconnect harness (20 runs): released-at-disconnect 20/20, loop errors [],
  close_count == 1 per stream
```

**Chaos journey (`tests/test_e2e_chaos.py`) — the capstone acceptance test.** A full Cursor session:
streaming `POST /v1/chat/completions` for `gpt-6.1-sol` with tool fields → target 1 answers `429`
`share_llm_error` → gateway fails over (no client disconnect) → target 2 answers `200` with a
multi-chunk SSE stream → follow-up non-streaming request routed straight to target 2 (cooldown) with
cache details. All four required verifications were proven **discriminating** by disabling each feature
and observing the test fail:

| Counterfactual | Result |
|---|---|
| baseline | passes |
| `sanitize_request_payload` → identity | **FAILS** at the tool-field assertion |
| `log_cache_stats` → no-op | **FAILS** at the `[CACHE HIT]` assertion |
| `CooldownRegistry.record_failure` → no-op | **FAILS** at the call-count assertion |

---

## 5. How to run

**Setup** (required before `python -m gateway` works — the project is src-layout):

```
python -m pip install -e .
```

Alternatively set `PYTHONPATH=src`. `config.yaml` is gitignored; copy the example and add real keys:

```
cp config.example.yaml config.yaml
```

**Run:**

```
python -m gateway                       # falls back to config.example.yaml when config.yaml is absent
python -m gateway --config config.yaml
sharellm-gateway --config config.yaml   # console script
```

Startup banner (for `config.example.yaml`):

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

**Client setup (Cursor / Cline):** set Base URL to `http://127.0.0.1:8000/v1` and use a model alias
from `routes[*].models` (e.g. `gpt-6.1-sol`). An unmapped model returns `404` with an OpenAI-style
`invalid_request_error` envelope.

---

## 6. Review process

Every task ran through an independent build/review loop: a Builder implements, a **fresh read-only
Reviewer** verifies against the spec and re-derives all evidence, findings go back to the Builder, and
the loop repeats until APPROVED. Five tasks, ten review rounds, all APPROVED.

Bugs caught by review that would otherwise have shipped:

| Task | Finding | Severity |
|---|---|---|
| 1.2 | A client disconnect while the stream generator was parked at a `yield` deferred the upstream close to the GC finalizer (**0/20** closed in time) and raised `RuntimeError("generator didn't stop after athrow()")` ~1/20 runs. Fixed to release at disconnect time (**20/20**). | MAJOR-class |
| 1.4 | The anti-422 guard stripped `tool_choice: "none"` even with a non-empty `tools` list, silently re-enabling tools the client had disabled. | **MAJOR** |
| 1.5 | The `python -m gateway` claim was inaccurate — the bare invocation failed with `No module named gateway`. Fixed with a console entry point + editable install + a subprocess test. | MINOR |

Also fixed across the sprint: a misleading failover log on the final target, an uncaught `TypeError`
on mixed-type provider keys, two tests that could not distinguish the error type they claimed to pin,
missing default-value and error-path coverage, and an unmade design decision (`target.model`) that was
resolved in Task 1.4.

---

## 7. Known limitations & disclosures

These are deliberate, disclosed, and none is a code defect.

1. **Streaming responses' own cache details are never logged.** Cache telemetry is emitted only for
   non-streaming responses. The gateway does not parse SSE bodies — streaming is zero-copy by design —
   so a `usage` block inside an SSE stream is relayed to the client but not extracted. The chaos test
   exercises the `[CACHE HIT]` line via a non-streaming follow-up.
2. **`python -m gateway` depends on an environment-level editable install.** `pip install -e .` was
   run into the shared managed venv (`~/.workbuddy-ai/binaries/python/envs/default`), which exposes the
   generically-named top-level package `gateway` globally. A fresh checkout has neither that install nor
   `PYTHONPATH=src` and will report `No module named gateway`. There is no README yet.
3. **The `uvicorn.run` serve path is never exercised by tests.** Tests drive the ASGI app in-process
   via `httpx.ASGITransport`; no test binds a real port. Startup binding is verified only by
   `--help` short-circuiting before `uvicorn.run`.
4. **Chaos verification 2 covers only the degenerate tool shape.** The sanitizer deliberately preserves
   a *non-empty* `tools` list, so the E2E journey demonstrates stripping only for the
   `tools: []` + `tool_choice: "none"` shape. The non-empty case is covered at unit level in
   `tests/test_adapters.py`.
5. **Only `route.targets[0]` semantics for model aliasing; `strategy` is not yet interpreted.**
   Targets are always walked in configured order; the `strategy: failover` value is carried but no
   other strategy (round-robin, etc.) is implemented.
6. **Cooldown is in-memory and per-process.** State does not survive a restart and is not shared
   across workers — a multi-worker deployment would cool each target independently.
7. **No retry/backoff within a target**, no request-level rate limiting, no auth on the gateway's own
   endpoints, and no metrics/tracing beyond logging.
8. **`RateLimit` matching is word-bounded but still heuristic** — a message containing the standalone
   word "limit" maps to `rate_limit_error` regardless of intent.
9. **Not covered by tests:** multi-worker behaviour, real upstream integrations, TLS, and
   large-payload/concurrency load characteristics.

---

## 8. Freeze record

**Tag:** `v0.1.0-milestone-1` (annotated) — see `git log` / `git tag -n` in the repository.

**Note on the freeze mechanism:** this workspace had **no version control** before the freeze
(`git rev-parse` returned `fatal: not a git repository`). A repository was therefore initialised as part
of this task so that "Milestone 1" is pinned to an immutable commit rather than only to the test gate.
The commit uses a **repository-local** identity (`ShareLLM Gateway Builder
<builder@sharellm-gateway.local>`) because no global git identity was configured — change it with
`git config user.name` / `user.email` if you want a different author on future commits.

**Artifact manifest** (SHA-256, first 16 hex chars):

| File | Lines | SHA-256 |
|---|---|---|
| `.gitignore` | 49 | `6c656d67451cda8d` |
| `config.example.yaml` | 51 | `65e12e3884a7bf25` |
| `pyproject.toml` | 41 | `327a58dac986e61f` |
| `src/gateway/__init__.py` | 27 | `bcad860f783d924d` |
| `src/gateway/__main__.py` | 17 | `9acc4c46d0a1738b` |
| `src/gateway/adapters/__init__.py` | 8 | `d938c8ea6100343e` |
| `src/gateway/adapters/sanitizer.py` | 92 | `0cc966bf3dd3d64d` |
| `src/gateway/adapters/sharellm.py` | 219 | `6da16961820e2c4d` |
| `src/gateway/config.py` | 261 | `cee462a0a5b5fdc6` |
| `src/gateway/engine/__init__.py` | 15 | `d5c33705b6f61a4a` |
| `src/gateway/engine/proxy.py` | 352 | `853464f472873147` |
| `src/gateway/engine/router.py` | 306 | `bb9272bfb45d6749` |
| `src/gateway/main.py` | 496 | `f84e2adb25501d3b` |
| `src/gateway/utils/__init__.py` | 6 | `06d115305f6a928d` |
| `tests/__init__.py` | 1 | `2058c1e8574b4eb1` |
| `tests/test_adapters.py` | 738 | `9e73a08c7003a366` |
| `tests/test_config.py` | 143 | `fd203d6c652d9c52` |
| `tests/test_e2e_chaos.py` | 791 | `0275f16e4e88f819` |
| `tests/test_failover.py` | 598 | `f6763d3192c13668` |
| `tests/test_proxy.py` | 586 | `00c5b60132c3cd7d` |
| **TOTAL** | **4,797** | |

The `analysis/` directory is committed alongside the code and holds the full sprint audit trail:
per-task specifications, Builder handoffs, evidence packages and every review verdict
(`analysis/agent_reviews/`).

---

## 9. Milestone 2 candidates

1. Interpret `route.strategy` beyond `failover` (round-robin, weighted, least-latency).
2. Persist/share cooldown state; per-target health endpoints.
3. Streaming cache telemetry (requires incremental SSE `usage` parsing — weigh against zero-copy).
4. Add a README documenting `pip install -e .`, plus request auth and rate limiting on the gateway's
   own endpoints.
5. Metrics/tracing (Prometheus/OpenTelemetry) and structured access logs.
