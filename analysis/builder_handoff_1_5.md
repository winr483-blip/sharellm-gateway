# Builder Handoff — Task 1.5

**Task:** Capstone — E2E Chaos Test, CLI Banner & Milestone 1 Freeze (ShareLLM Smart Gateway)
**Status:** `COMPLETE`

---

## 1. Files created / changed

| Path (absolute) | Change | One-line purpose |
|---|---|---|
| `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway/src/gateway/adapters/sharellm.py` | **Modified** | F2: rate-limit keywords are now matched as whole words (`\b(limit\|limits\|overloaded)\b`, case-insensitive) instead of unanchored substrings. |
| `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway/src/gateway/main.py` | **Modified** | Factored the CLI into pure, testable functions `parse_args` / `resolve_config_path` / `format_startup_banner`; rewrote `run_cli` on top of them; added CLI constants; exported the new functions. |
| `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway/src/gateway/__main__.py` | **Created** | `python -m gateway` entry point; calls `gateway.main.run_cli()`. |
| `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway/pyproject.toml` | **Modified** (round 0) | Added `[project.scripts] sharellm-gateway = "gateway.main:run_cli"` so the project declares a runnable console command. |
| `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway/src/gateway/engine/__init__.py` | **Modified** (round 1) | Replaced the stale "intentionally empty" docstring with an accurate description of `proxy.py` and `router.py` (NIT-6). |
| `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway/tests/test_e2e_chaos.py` | **Created** | E2E chaos journey (legs A + B), F2 regression guard, zero-leak disconnect check, CLI pure-function tests, and a real `subprocess` `python -m gateway --help` test. |

**Not touched:** `tests/test_adapters.py`, `tests/test_config.py`, `tests/test_failover.py`,
`tests/test_proxy.py`, `config.example.yaml`, everything under `analysis/`
(except this mandated handoff file).

---

## 2. Exact diff of any existing test touched

**None.** No existing test file was modified. `tests/test_adapters.py` is byte-identical to the
approved Task 1.4 revision (mtime `21:44:23`, unchanged; sha256
`9e73a08c7003a366dd69865ad1e358558213d6450281a3b3cc3870eca889f2e7`).

### F2 stale-assertion check (required by the task)

I checked every existing assertion in `tests/test_adapters.py` that exercises
`sanitize_error_envelope`, by evaluating both the old and the new matcher against each message
that appears in the file:

```
503 'server overloaded'                  old_rate=True  new_rate=True
400 'you hit the rate limit'             old_rate=True  new_rate=True
429 'Rate limit reached for gpt-6.1-sol' old_rate=True  new_rate=True
400 'bad field'                          old_rate=False new_rate=False
500 'boom'                               old_rate=False new_rate=False
429 'primary rate limited'               old_rate=True  new_rate=True
429 'backup rate limited'                old_rate=True  new_rate=True
429 'rate limited'                       old_rate=True  new_rate=True
400 'unlimited tokens available'         old_rate=True  new_rate=False  <-- DIFF (the intended F2 fix)
```

**Conclusion:** no existing assertion pins the old *unanchored* behaviour. The only message whose
classification changes is `"unlimited tokens available"` — which does not appear in any existing
test. `test_rate_limit_message_overrides_the_status` (the closest case: a 400 whose message
contains `"limit"` / a 503 with `"overloaded"`) uses whole-word occurrences, so it passes unchanged
under both matchers. Empirically, the full suite was green before and after the F2 change
(61 → 61, then 75 with the new file). I therefore left `tests/test_adapters.py` untouched, and placed
the F2 regression guard in the new capstone test file instead (see §4).

---

## 3. Key design decisions

### 3.1 CLI factoring (`src/gateway/main.py`)

The CLI is split into the four functions the spec names, so every piece is testable without a
server:

* `parse_args(argv: Sequence[str]) -> argparse.Namespace` — a `prog="gateway"` parser with a single
  optional `--config PATH` (default `None`).
* `resolve_config_path(explicit: str | Path | None) -> Path` — precedence:
  1. explicit `--config` → used verbatim; `FileNotFoundError` (message contains `--config`) if it is
     not a file;
  2. `config.yaml` in the CWD;
  3. `config.example.yaml` in the CWD;
  4. otherwise `FileNotFoundError` naming **both** candidates.
  `config.yaml` is gitignored and absent in this repo, so path 3 is the normal case here.
* `format_startup_banner(config: AppConfig) -> str` — **pure**, returns a string, prints nothing.
* `run_cli(argv: Sequence[str] | None = None) -> None` — `parse_args` → `resolve_config_path` →
  `load_config` → `print(banner)` → `uvicorn.run(...)`. This is the only code path that binds a
  socket.

`__main__.py` simply calls `run_cli()`. `python -m gateway --help` is handled entirely by argparse,
which raises `SystemExit(0)` **before** `uvicorn.run` is ever reached — so no port is bound.

Note on invocation: the project is `src/`-layout, so `python -m gateway` only works when the
`gateway` package is **importable**. That is satisfied by either:

* **`pip install -e .`** (recommended) — installs the package in editable mode, after which the
  **bare** `python -m gateway …` works from the project root with no `PYTHONPATH`; or
* **`PYTHONPATH=src`** — e.g. `PYTHONPATH=src python -m gateway …`.

Without one of these, a bare `python -m gateway` fails with `No module named gateway` (the
project root contains `src/`, not `gateway/`). The editable install also provides the
`sharellm-gateway` console script declared in `[project.scripts]`. Both paths are verified in §4.

### 3.2 Banner layout (ASCII only)

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

`=` rules and `-`/`>`/`:`/`,` only — `banner.isascii()` is asserted in the test suite.

**Tip parameterisation decision.** The spec gives the tip as the literal
`Set Base URL to http://127.0.0.1:8000/v1`. I derive that URL from the configured
`host`/`port` (`f"http://{host}:{port}/v1"`) so the tip and the "Gateway URL" line can never
disagree. For `config.example.yaml` (host `127.0.0.1`, port `8000`) the rendered tip is
**byte-identical** to the spec's literal string; for any other config the tip stays correct instead
of pointing at a stale default. This is the only place I deviated from a literal reading of the spec,
and the reference output is unaffected.

### 3.3 Chaos-journey leg structure (`tests/test_e2e_chaos.py`)

A single `respx.mock` context covers the whole session so call counts accumulate across both legs.
Target 2's mock uses a `side_effect` responder that dispatches on the request's `stream` flag: an SSE
stream for leg A, a cached JSON body for leg B. Assertions are made on **both** the client-visible
responses and the **captured upstream request bodies** (`route.calls[i].request.content`).

* **Leg A (streaming).** Inbound: `model="gpt-6.1-sol"`, a real `messages` list, plus the empty tool
  fields the anti-422 guard strips (`"tools": []`, `"tool_choice": "none"`), `"stream": true`.
  Target 1 (`sharellm_primary`) → `429` `{"error": {"message": "channel rate limit exceeded",
  "type": "share_llm_error"}}`. Target 2 (`openrouter_backup`) → `200` multi-chunk SSE (with an
  embedded cache-usage chunk). Asserts: client sees `200` + byte-exact `text/event-stream`; target 2's
  captured body `== {"model": "gpt-6-astra", "messages": <intact>, "stream": True}` (tool fields
  **absent**, `set(body) == {"model","messages","stream"}`); target 1's captured body keeps
  `"model": "gpt-6.1-sol"`.
* **Leg B (non-streaming follow-up).** `model="gpt-6.1-sol"`, `"stream": false`. Target 1 is cooling
  down, so `_select_targets` routes straight to target 2, which returns
  `usage.prompt_tokens_details.cached_tokens = 3840 / prompt_tokens = 4387`. The `[CACHE HIT] …` line
  is emitted and captured. Asserts: cooling flags (`primary` cooling, `backup` not), and across the
  whole journey `primary.call_count == 1`, `backup.call_count == 2`.

`app.state.cooldown_registry.reset()` is called at the start of the journey and of the disconnect
loop so the file is not order-dependent.

### 3.4 Cache-telemetry interpretation (explicit spec instruction)

Implemented exactly as the spec's "Resolution of the cache-telemetry ambiguity" dictates: **no SSE
body parsing was added.** The gateway's streaming path stays zero-copy. The `[CACHE HIT] …` line is
emitted from **leg B's non-streaming** request, via the existing Task 1.4 path
(`extract_cache_stats` → `log_cache_stats` in `main.py`). The test attaches a real
`logging.Handler` to the `gateway.adapters.sharellm` logger (with the logger level raised to `INFO`
for the duration and restored afterwards) and asserts the **message content** of the captured
`LogRecord`s, not merely that something was logged:

```
EXPECTED: "[CACHE HIT] 3840/4387 tokens (87.5%) via openrouter_backup"
```

### 3.5 Where the CLI tests live

The task's file list creates exactly one new test file (`tests/test_e2e_chaos.py`) and forbids
modifying `test_adapters.py` unless an assertion is stale (none is). The CLI pure-function tests
therefore live in the same capstone module, in a clearly labelled `CLI` section. `run_cli` is covered
by a single test that monkeypatches `uvicorn.run`, so nothing binds a port.

---

## 4. Verification evidence (real output)

### V1 — Full suite (`pytest -v` from the project root)

```
============================= 81 passed in 6.37s ==============================
```

Per-file collection counts (from `pytest <file> --collect-only -q`):

```
tests/test_adapters.py  -> 36 tests collected
tests/test_config.py    ->  3 tests collected
tests/test_e2e_chaos.py -> 20 tests collected   (new)
tests/test_failover.py  -> 13 tests collected
tests/test_proxy.py     ->  9 tests collected
```

61 pre-existing tests remain green; the new file adds 20. New-file test names:

```
test_chaos_journey_streaming_failover_then_cached_follow_up PASSED
test_rate_limit_match_is_word_bounded PASSED
test_rate_limit_word_boundary_guard_end_to_end PASSED
test_client_disconnect_during_failover_releases_every_stream PASSED
test_parse_args_defaults_config_to_none PASSED
test_parse_args_reads_explicit_config PASSED
test_parse_args_help_exits_without_error PASSED
test_resolve_config_path_uses_explicit_path PASSED
test_resolve_config_path_rejects_missing_explicit_path PASSED
test_resolve_config_path_prefers_config_yaml PASSED
test_resolve_config_path_falls_back_to_example PASSED
test_resolve_config_path_raises_when_neither_exists PASSED
test_format_startup_banner_is_pure_ascii_and_informative PASSED
test_run_cli_prints_banner_without_binding_a_port PASSED
test_python_dash_m_gateway_help_runs_as_a_child_process PASSED
test_parse_args_program_name_reflects_invocation[...] PASSED   (5 parametrisations)
```

### V2 — mypy

```
$ python -m mypy
Success: no issues found in 11 source files

$ python -m mypy --strict src/gateway
Success: no issues found in 11 source files
```

(11 source files = the previous 10 plus the new `src/gateway/__main__.py`.)

### V3 — `python -m gateway --help` and the `sharellm-gateway` console script

**Bare invocation, no `PYTHONPATH`, after `pip install -e .`** (see V8):

```
$ python -m gateway --help
usage: gateway [-h] [--config PATH]

ShareLLM Smart Gateway - a typed, multi-provider LLM routing gateway with
automatic failover.

options:
  -h, --help     show this help message and exit
  --config PATH  Path to the YAML configuration file. Defaults to config.yaml,
                 falling back to config.example.yaml when config.yaml is
                 absent.
exit=0
```

**Console script** (installed by `pip install -e .` from `[project.scripts]`; the `usage:` line
names the command it was invoked as — NIT-1, round 1):

```
$ sharellm-gateway --help
usage: sharellm-gateway [-h] [--config PATH]

ShareLLM Smart Gateway - a typed, multi-provider LLM routing gateway with
automatic failover.

options:
  -h, --help     show this help message and exit
  --config PATH  Path to the YAML configuration file. Defaults to config.yaml,
                 falling back to config.example.yaml when config.yaml is
                 absent.
exit=0
```

**No port was bound.** `--help` is handled by argparse, which raises `SystemExit(0)` before
`run_cli` reaches `uvicorn.run`. Confirmed two ways: (a) `run_cli(["--help"])` under a
`sys.addaudithook` recording `socket.bind` / `socket.connect` yields `SYSTEM_EXIT_CODE 0` and
`SOCKET_EVENTS_DURING_HELP []`; and (b) the new subprocess test
`test_python_dash_m_gateway_help_runs_as_a_child_process` runs the real child process and asserts
exit 0 + usage on stdout + a prompt exit (~1.3 s measured; a bound, blocking server would hit the
30 s timeout instead).

### V4 — Rendered banner for `config.example.yaml` (verbatim, from `format_startup_banner`)

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
isascii: True
```

### V5 — F2 before / after

"Before" is the pre-change logic reproduced verbatim (`_RATE_LIMIT_MARKERS = ("limit", "overloaded")`
+ `any(marker in lowered ...)`); "after" is the shipped `sanitize_error_envelope`:

```
status=400 message='unlimited tokens available'
  BEFORE: {'error': {'message': 'unlimited tokens available', 'type': 'rate_limit_error'}}
  AFTER : {'error': {'message': 'unlimited tokens available', 'type': 'invalid_request_error'}}

status=400 message='rate limit exceeded'
  BEFORE: {'error': {'message': 'rate limit exceeded', 'type': 'rate_limit_error'}}
  AFTER : {'error': {'message': 'rate limit exceeded', 'type': 'rate_limit_error'}}
```

### V6 — `tests/test_config.py` unchanged

```
bytes: 4859
sha256: fd203d6c652d9c523c2f6840e7e45bd9e08cf347aad2e548ed5152b77c1a2f90
prefix matches fd203d6c652d9c52: True
```

### V7 — Socket audit over the suite (no fixed ports bound)

The whole suite was run in-process under a `sys.addaudithook` recording `socket.bind` /
`socket.connect`:

```
PYTEST_EXIT_CODE 0
TOTAL_BINDS 27
TOTAL_CONNECTS 27
DISTINCT_BIND_ADDRESSES ["('127.0.0.1', 0)"]
FIXED_PORT_BINDS []
NO_FIXED_PORT_BOUND True
```

Every bind is to **ephemeral port 0** on loopback — i.e. the OS-assigned socketpairs asyncio's
Windows event loop uses for its self-pipe, one per event loop. A control run of the sync-only
`tests/test_config.py` under the same hook produced **zero** bind/connect events, confirming these
are event-loop infrastructure, not the gateway. No fixed port (8000 or otherwise) is ever bound by
the suite.

### V8 — Editable install (round 1)

```
$ python -m pip install -e .
...
Building editable for sharellm-gateway (pyproject.toml): finished with status 'done'
Created wheel for sharellm-gateway: filename=sharellm_gateway-0.1.0-0.editable-py3-none-any.whl size=1807 sha256=4a91466a1be5d8e0c668c4caaaebed33b739bbcc9a5185c8498f21043e106659
Successfully built sharellm-gateway
Installing collected packages: sharellm-gateway
Successfully installed sharellm-gateway-0.1.0
PIP_EXIT=0
```

The install created `src/sharellm_gateway.egg-info/`, which is covered by `.gitignore` line 26
(`*.egg-info/`) — confirmed:

```
$ ls src/
gateway  sharellm_gateway.egg-info

$ grep -n "egg-info" .gitignore
26:*.egg-info/
```

After the install, the **bare** `python -m gateway --help` (no `PYTHONPATH`) works — see V3.

---

## 5. Known limitations / follow-ups

* **A streaming response's own cache details are not logged.** This is deliberate and matches the
  spec's resolution: the gateway does not parse SSE bodies (zero-copy streaming), so
  `usage.prompt_tokens_details.cached_tokens` embedded in an SSE stream is relayed to the client but
  never extracted. Cache telemetry is emitted only for **non-streaming** responses. The chaos test
  therefore exercises the cache line through leg B (a non-streaming follow-up), not through the
  streaming leg A. If streaming cache telemetry is ever wanted it needs an SSE parser, which the spec
  explicitly forbids here.
* **`python -m gateway` needs the package importable.** The project is `src/`-layout, so a bare
  `python -m gateway` fails with `No module named gateway` unless the package is installed
  (`pip install -e .`, which also provides the `sharellm-gateway` console script) or `src` is on
  `sys.path` (`PYTHONPATH=src`). Both are documented in §3.1 and verified in §4 (V3/V8). The
  subprocess test sets `PYTHONPATH=src` so it passes with or without the editable install.
* **The banner tip is parameterised** from `host`/`port` rather than hard-coded to
  `127.0.0.1:8000` (see §3.2). Reference output is identical; flagged here in case the Reviewer
  prefers the literal string.
* **CLI tests live in `tests/test_e2e_chaos.py`** rather than a dedicated `tests/test_cli.py`, because
  the task's file list creates only that one new test file. Trivial to split later if preferred.
* `run_cli` is covered with `uvicorn.run` monkeypatched (in-process), plus one real child-process
  test that runs `python -m gateway --help` via `subprocess`. The real `uvicorn.run` *serve* path
  (binding a port) is never exercised in the suite — by design.

---

## 6. Evidence package (minimal reviewer reading list)

1. `src/gateway/adapters/sharellm.py` — the F2 matcher (`_RATE_LIMIT_PATTERN`, `_normalized_error_type`).
2. `src/gateway/main.py` — lines ~55–70 (CLI constants) and ~316–end (`parse_args`,
   `resolve_config_path`, `format_startup_banner`, `run_cli`).
3. `src/gateway/__main__.py` — the entry point.
4. `pyproject.toml` — `[project.scripts] sharellm-gateway = "gateway.main:run_cli"`.
5. `tests/test_e2e_chaos.py` — the whole new file.

---

## FIXES APPLIED (round 0 — pre-review)

Reviewer finding: **the `python -m gateway` claim was inaccurate** — a bare `python -m gateway --help`
from the project root (no `PYTHONPATH`) fails with `No module named gateway`, and the handoff implied
it worked out of the box. All four sub-items plus the STATUS correction are addressed below.

| # | Finding | Concrete change | Evidence |
|---|---|---|---|
| 1 | Add a console-script entry point. | Added to `pyproject.toml`: `[project.scripts]` → `sharellm-gateway = "gateway.main:run_cli"`. `run_cli` is already callable with no arguments. | `pyproject.toml`; `sharellm-gateway --help` → exit 0 (V3). |
| 2 | Verify the editable install and report the real result; confirm `egg-info` is gitignored. | Ran `python -m pip install -e .` exactly as given. **It succeeded** (`PIP_EXIT=0`, `Successfully installed sharellm-gateway-0.1.0`). After it, the **bare** `python -m gateway --help` (no `PYTHONPATH`) works and exits 0. `src/sharellm_gateway.egg-info/` was created and is matched by `.gitignore:26` (`*.egg-info/`). | §4 V8 (install output, `ls src/`, gitignore rule) and §4 V3 (bare invocation). |
| 3 | Add a real subprocess CLI test. | Added `test_python_dash_m_gateway_help_runs_as_a_child_process` to `tests/test_e2e_chaos.py`: runs `[sys.executable, "-m", "gateway", "--help"]` via `subprocess.run` with `PYTHONPATH` set to the project `src` dir (deterministic with or without the editable install), `cwd=PROJECT_ROOT`, `timeout=30`. Asserts `returncode == 0`, `usage: gateway` / `--config` / `config.example.yaml` on stdout, empty stderr, and a prompt exit (`elapsed < 15.0`, measured ≈1.3 s) — which is the no-port-bound guarantee, since a bound blocking server would hit the timeout instead. No sleep, no socket bound. | `tests/test_e2e_chaos.py`; passes (V1). |
| 4 | Correct the handoff. | Replaced the bare claim in §3.1 with the precise requirement: `python -m gateway` needs the `gateway` package importable, satisfied by `pip install -e .` (recommended, verified) **or** `PYTHONPATH=src`; documented the `No module named gateway` failure otherwise. Rewrote §4 V3 (bare invocation + console script) and §5, and added §4 V8 (editable-install output verbatim). | this file, §3.1 / §4 V3 / §4 V8 / §5. |
| 5 | `STATUS:` should be `COMPLETE` (first round). | Changed the header to `**Status:** \`COMPLETE\``. | line 4. |

### Round-0 verification results

* **Full suite:** `76 passed in 5.40s` (75 prior + 1 new subprocess test). Per-file: adapters 36,
  config 3, e2e_chaos **15**, failover 13, proxy 9.
* **Bare `python -m gateway --help` after `pip install -e .`:** exit 0, usage printed (V3).
* **`sharellm-gateway --help`:** exit 0, usage printed (V3).
* **`python -m mypy --strict src/gateway`:** `Success: no issues found in 11 source files`.
* **`tests/test_config.py`:** byte-identical — 4859 bytes, sha256
  `fd203d6c652d9c523c2f6840e7e45bd9e08cf347aad2e548ed5152b77c1a2f90` (V6).
* **Socket audit:** unchanged — `FIXED_PORT_BINDS []`, `NO_FIXED_PORT_BOUND True` (V7).

No existing test was weakened or modified; the only test-file change is the added subprocess test.
`mypy` stayed clean because `src/gateway` sources were not changed in this round (only `pyproject.toml`
and the test module).

---

## FIXES APPLIED (round 1)

Milestone-1 freeze polish. Reviewer verdict on the prior round was **APPROVED** (no BLOCKER, no
MAJOR); this round closes four polish nits so the frozen artifact is clean. The section above was
renamed to `FIXES APPLIED (round 0 — pre-review)` for disambiguation.

Explicitly **not** changed, per instruction: NIT-4 (the `elapsed < 15.0` bound — left as is), NIT-5
(spec-internal tension — no change), MINOR-1 (git init/commit/tag — the Reviewer's job; no git
command was run and no git files were added), MINOR-2 (the editable install — left in place).

| Nit | Finding | Concrete change | Evidence |
|---|---|---|---|
| **NIT-6** | `src/gateway/engine/__init__.py` docstring claimed the package "is intentionally empty for now" — false since Tasks 1.2/1.3. | Rewrote the docstring to describe what the package holds: `proxy.py` (hop-by-hop header stripping; buffered forwarding + TTFT-inspected streaming) and `router.py` (ordered target selection, retryable policy, `CooldownRegistry`). | new docstring quoted in §"NIT-6 docstring" below. |
| **NIT-6 (audit)** | Check `adapters/__init__.py` and `utils/__init__.py` for the same staleness. | `adapters/__init__.py` is **accurate** (already names `sharellm.py` + `sanitizer.py`) — left unchanged. `utils/__init__.py` is genuinely still empty — left unchanged. | source inspection. |
| **NIT-1** | `main.py` hard-coded `prog="gateway"`, so the console script printed `usage: gateway`. | Added `_program_name()`: derives `prog` from `Path(sys.argv[0]).name`, strips a trailing `.exe`, and falls back to `"gateway"` for a non-meaningful basename (`__main__.py`, empty, `-m`, `-c`). `parse_args` now passes `prog=_program_name()`; the rule is documented in both docstrings. | `python -m gateway --help` → `usage: gateway …`; `sharellm-gateway --help` → `usage: sharellm-gateway …` (V3). |
| **NIT-3** | The banner "purity" test only proved idempotence — it would pass even if `format_startup_banner` printed. | Added `capsys` to `test_format_startup_banner_is_pure_ascii_and_informative` and asserted `captured.out == ""` and `captured.err == ""` after both calls; kept the ASCII and content assertions. | `tests/test_e2e_chaos.py`; passes. |
| **NIT-2** | Module docstring claimed *every* expected URL/model/provider is derived, but `:656`/`:690` hard-coded `http://127.0.0.1:8000/v1`. | **Chose the "derive" option** (the Reviewer's stated preference): added `EXPECTED_BASE_URL = f"http://{EXAMPLE_CONFIG.gateway.host}:{EXAMPLE_CONFIG.gateway.port}/v1"` and used it in both the banner test and the `run_cli` test (the tip assertion becomes `f"Set Base URL to {EXPECTED_BASE_URL}"`). Also tightened the docstring to state precisely which values are derived and which are the few literal inputs. | `tests/test_e2e_chaos.py`; passes. |

### NIT-6 docstring (new `src/gateway/engine/__init__.py`, verbatim)

```python
"""Execution engine for the ShareLLM Smart Gateway.

Two modules make up the engine:

* :mod:`gateway.engine.proxy` — the transparent forwarding core
  (:func:`~gateway.engine.proxy.forward_request`): it strips hop-by-hop headers
  before forwarding, and relays one payload either as a fully buffered JSON
  response or as a raw byte stream whose first chunk is inspected (TTFT) before
  the caller's stream commits.
* :mod:`gateway.engine.router` — the resilient failover router
  (:func:`~gateway.engine.router.dispatch`): it walks a route's ordered targets,
  treats retryable statuses (429/5xx) and transport errors as failures, and
  skips recently failed targets through a per-application
  :class:`~gateway.engine.router.CooldownRegistry`.
"""
```

### Round-1 verification results

* **Full suite:** `81 passed in 6.37s`. Per-file: adapters 36, config 3, **e2e_chaos 20**,
  failover 13, proxy 9. (Round 0 was 76; the +5 are the five parametrisations of the new
  `test_parse_args_program_name_reflects_invocation`.)
* **`python -m gateway --help`:** exit 0, `usage: gateway [-h] [--config PATH]`.
* **`sharellm-gateway --help`:** exit 0, `usage: sharellm-gateway [-h] [--config PATH]`.
* **`python -m mypy --strict src/gateway`:** `Success: no issues found in 11 source files`.
* **`tests/test_config.py`:** byte-identical — 4859 bytes, sha256
  `fd203d6c652d9c523c2f6840e7e45bd9e08cf347aad2e548ed5152b77c1a2f90`.
* **Socket audit:** unchanged — `FIXED_PORT_BINDS []`, `NO_FIXED_PORT_BOUND True`.

No existing test was weakened. The only test-file changes are: the `capsys` purity assertion
(NIT-3), the derived-URL assertions (NIT-2), the `sys.argv` pin + new parametrised test (NIT-1), and
the docstring correction (NIT-2). §4 V1 and V3 above were refreshed to the current artifact's real
output so the frozen handoff is internally consistent.
