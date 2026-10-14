# Evidence Package — Task 1.5 (Review Round 1)

## 1. Task specification

`analysis/task-1.5.md` — "Task 1.5 — Capstone: E2E Chaos Test, CLI Banner & Milestone 1 Freeze".
Read it in full, including "Resolution of the cache-telemetry ambiguity" and "Testability
requirement for the CLI".

## 2. Project root

    C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway

## 3. Builder handoff

`analysis/builder_handoff_1_5.md` — STATUS: COMPLETE (see `## FIXES APPLIED (round 1)`).

## 4. Files created / changed

    src/gateway/__main__.py            (created)  `python -m gateway` entry point
    tests/test_e2e_chaos.py            (created)  15 tests: chaos journey, F2 guards, disconnect, CLI
    src/gateway/adapters/sharellm.py   (modified) F2: word-boundary rate-limit match
    src/gateway/main.py                (modified) CLI: parse_args / resolve_config_path / format_startup_banner / run_cli
    pyproject.toml                     (modified) [project.scripts] sharellm-gateway = "gateway.main:run_cli"

**No existing test file was modified.** `tests/test_config.py` byte-identical throughout.
No git repo existed when the work was done, so byte-diffs are unavailable — use content + hashes.

## 5. Reviewer reading list (priority order)

1. `tests/test_e2e_chaos.py` — the capstone artifact
2. `src/gateway/main.py` (CLI section)
3. `src/gateway/adapters/sharellm.py` (F2)
4. `src/gateway/__main__.py`
5. `pyproject.toml`
6. `analysis/task-1.5.md`

## 6. Build / test output

    cd "C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway" && \
      C:/Users/Notebook/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe -m pytest -q

    76 passed in 5.74s

Per-file: adapters 36, config 3, e2e_chaos 15, failover 13, proxy 9.

`python -m mypy --strict src/gateway` → `Success: no issues found in 11 source files`.
`tests/test_config.py` → 4859 bytes, SHA-256 prefix `fd203d6c652d9c52` (unchanged).

## 7. Orchestrator's independent probe

**Bare `python -m gateway --help`** (no `PYTHONPATH`, from the project root) — after an
editable install (`pip install -e .`) performed by the Builder:

    usage: gateway [-h] [--config PATH]

    ShareLLM Smart Gateway - a typed, multi-provider LLM routing gateway with
    automatic failover.

    options:
    ...
    exit=0

**Console script** `sharellm-gateway --help` → same usage, `exit=0`.

**Rendered banner** for `config.example.yaml`:

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

**F2 fix** (`sanitize_error_envelope`, `share_llm_error` type):

    400 'unlimited tokens available' -> invalid_request_error   <- the fix
    429 'unlimited tokens available' -> rate_limit_error        (429 always rate-limits)
    400 'you hit the rate limit'     -> rate_limit_error
    400 'server overloaded'          -> rate_limit_error
    400 'limits exceeded'            -> rate_limit_error
    400 'no limit here'              -> rate_limit_error        (whole word "limit" present)

## 8. Specific items to adjudicate

1. **Is the chaos test a genuine end-to-end journey, or a unit test in disguise?** It runs the real
   ASGI app via `create_app` with `respx` mocking both upstreams, and asserts on the **captured
   upstream request bodies** (what target 1 and target 2 actually received) as well as the
   client-visible responses. Judge whether all four required verifications are really proven:
   (1) 200 + unbroken SSE; (2) sanitizer stripped tool fields before target 2; (3) cache log emitted;
   (4) cooldown applied to target 1 for subsequent requests.

2. **The cache-telemetry interpretation.** The spec resolves the streaming/non-streaming tension by
   having the journey's leg B (a non-streaming follow-up) carry the cache details, so verification 3
   and verification 4 both come from subsequent requests. Confirm this matches the spec's stated
   resolution and that no SSE body parsing was added.

3. **The `python -m gateway` gap I found.** The Builder's first-round handoff claimed the bare
   invocation worked when it only worked with `PYTHONPATH=src`. It has been corrected, a
   `[project.scripts]` entry point added, an editable install performed and verified, and a
   subprocess test added. Confirm the claim is now accurate and that the subprocess test is
   deterministic **with and without** the editable install present.

4. **The editable install is a persistent change to the shared venv** (`envs/default`) and created
   `src/sharellm_gateway.egg-info/`. Confirm that directory is covered by `.gitignore` (`*.egg-info/`)
   and flag if you consider the install problematic.

5. **`resolve_config_path` semantics.** Explicit `--config` used as given (clear error if missing);
   no flag → `config.yaml` if present, else `config.example.yaml`, else a clear error naming both.
   `config.yaml` is gitignored and absent in this repo, so the fallback is the normal path.

6. **Banner purity.** `format_startup_banner` must be pure (no printing) and ASCII-only. Confirm no
   non-ASCII characters, and that `run_cli` does the printing.

7. **No test may call `run_cli` in-process** — it binds a port and blocks. Confirm the CLI tests use
   the pure functions, and that the one subprocess test cannot hang (bounded timeout).

## 9. Reviewer instruction

Verify against `analysis/task-1.5.md` yourself. Do not take the Builder's claims or the
orchestrator's probe on trust — re-read the files, write your own probes, and re-run the suite.
Report a structured verdict.
