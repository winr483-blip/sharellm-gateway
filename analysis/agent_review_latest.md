# Review — Task 1.5 (Round 1)

STATUS: APPROVED

Reviewer: independent read-only review (fresh agent instance, no prior context).  
Note: the configured Reviewer model `Atria-Dawn-Preview` was unavailable (429 token quota  
exhausted), so this round ran on the `hy4-preview-f` model with the Reviewer agent's read-only  
tool restrictions intact.

## Spec compliance

| Requirement                                                                                | Verdict                           | Evidence                                                                                                                                                                                          |
| ------------------------------------------------------------------------------------------ | --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| §1 F2 — `r"\b(limit\|limits\|overloaded)\b"`, case-insensitive                             | **MET**                           | `sharellm.py:60` `_RATE_LIMIT_PATTERN = re.compile(...)`, consumed at `:108`; old `_RATE_LIMIT_MARKERS` gone from `src/`                                                                          |
| §1 F2 — `400 "unlimited tokens available"` must not map to `rate_limit_error`              | **MET**                           | 16-case probe: `400 'unlimited tokens available' → invalid_request_error`; `'rate limit exceeded'` / `'server overloaded'` / `'limits exceeded'` / `'Limit'` / `'pre-limit'` → `rate_limit_error` |
| §2 `__main__.py` exists; `python -m gateway` runs                                          | **MET** (documented prerequisite) | `__main__.py:14-17`; bare `python -m gateway --help` → exit 0                                                                                                                                     |
| §2 `--config` with `config.yaml` → `config.example.yaml` fallback                          | **MET**                           | `main.py:338-346`, `:370-391`; all four branches covered by tests                                                                                                                                 |
| §2 banner: URL, routes **with fallback targets**, Cursor/Cline tip, ASCII-only             | **MET**                           | `main.py:394-439`; rendered banner verified; `isascii: True`; 2 routes × 2 targets with provider→model + timeouts                                                                                 |
| §3 chaos test simulates the journey and proves all four verifications                      | **MET**                           | `test_e2e_chaos.py:225-351`; all four proven **and discriminating** (counterfactuals below)                                                                                                       |
| §4 full suite, `mypy --strict`, zero socket leaks                                          | **MET**                           | `76 passed`; `Success: no issues found in 11 source files`; 20-run disconnect probe → 20/20 released at disconnect, one close per stream, `loop errors: []`                                       |
| Objective 4 — "Freeze and **tag** Milestone 1"                                             | **PARTIAL**                       | No `.git` anywhere in the tree or parents, so no tag exists. The handoff never addressed the "tag" half — see MINOR-1. **Resolved by the orchestrator post-review** (see below)                   |
| Cache-telemetry resolution — leg A streaming, leg B non-streaming; **no SSE body parsing** | **MET**                           | `test_e2e_chaos.py:277-293`; grep of `src/` finds no `aiter_text`/`aiter_lines`/`data: ` parsing; limitation documented                                                                           |
| Testability — pure `parse_args`/`resolve_config_path`/`format_startup_banner`              | **MET**                           | `main.py:317/350/394/442`; all three directly tested                                                                                                                                              |
| Constraint — all 61 existing tests green; `test_config.py` untouched                       | **MET**                           | 76 = 36+3+15+13+9; per-file counts identical to the 1.4 baseline; `test_config.py` 4859 B `fd203d6c652d9c52`                                                                                      |
| Constraint — deterministic, zero-network-bound                                             | **MET**                           | Audit hook: `TOTAL_BINDS 27`, `DISTINCT_BINDS ["('127.0.0.1', 0)"]`, `LISTENS 0`, `NON_EPHEMERAL_BINDS []`                                                                                        |
| Constraint — "Never call `run_cli()` from a test"                                          | **PARTIAL (NIT)**                 | One test calls `run_cli([])` with `uvicorn.run` monkeypatched — permitted by the spec's Testability clause; intent satisfied                                                                      |
| Constraint — ASCII only in the banner                                                      | **MET**                           | `banner.isascii() is True`                                                                                                                                                                        |

## Findings

No BLOCKER and no MAJOR findings.

**MINOR-1 — the "tag Milestone 1" objective is unaddressed.** No VCS exists (`git rev-parse` →  
`fatal: not a git repository`), so no tag could be created, and the handoff never said so. Resolved by  
the orchestrator after this review: a git repository was initialised, the tree committed and an  
annotated Milestone 1 tag created (see the release report).

**MINOR-2 — environment mutation: `pip install -e .` into the shared `envs/default` venv.**  
`__editable__.sharellm_gateway-0.1.0.pth` + `sharellm_gateway-0.1.0.dist-info` now exist; the installed  
top-level import name is the generic `gateway`, now globally resolvable inside a **shared** venv  
(collision risk with any other project shipping a `gateway` package). Consequences: a fresh checkout  
still fails with `No module named gateway` without the install or `PYTHONPATH=src`, and there is no  
README documenting the setup step. Mitigating: the install pulled no new distributions,  
`src/sharellm_gateway.egg-info/` is covered by `.gitignore:26`, and the dependency is documented.  
Non-blocking — **disclosed in the Milestone 1 release report.**

**NIT-1 — `main.py:332` — `prog="gateway"` hardcoded**, so the console script prints `usage: gateway`  
even when invoked as `sharellm-gateway`.

**NIT-2 — `test_e2e_chaos.py:19-21` docstring** claims every expected URL is derived from  
`config.example.yaml`, but `:656`/`:690` hard-code `http://127.0.0.1:8000/v1`. Doc/impl mismatch.

**NIT-3 — `test_e2e_chaos.py:646-666`** asserts idempotence, which does not prove "no printing"  
(no `capsys`). Purity holds by source inspection; a `capsys` assertion would close the gap.

**NIT-4 — `test_e2e_chaos.py:734` — `assert elapsed < 15.0` is a wall-clock assertion.** Measured  
1.54 s (≈10× margin), so acceptable; the only environment-sensitive assertion in the file.

**NIT-5 — spec-internal tension, resolved sensibly.** §Constraints says never call `run_cli()` in a  
test; §Testability permits it with `uvicorn.run` monkeypatched. One test does the latter; the socket  
audit proves nothing binds.

**NIT-6 (pre-existing, outside this task's file set) — `src/gateway/engine/__init__.py:1-7`** still says  
the engine package "is intentionally empty for now" while it now contains `proxy.py` and `router.py`.  
Stale since Task 1.3; noted because this is the freeze milestone.

## Adjudications

**§8.1 — Genuine E2E, not a unit test in disguise.** It builds the real app (`create_app`,  
`test_e2e_chaos.py:208`), drives it via `httpx.ASGITransport` (`:212-215`), and lets the real  
`main → router.dispatch → proxy.forward_request` stack run; only the network boundary is intercepted.  
Assertions are on **both** client-visible responses and captured upstream bodies. All four  
verifications proven **discriminating** by counterfactual monkeypatching:

| Counterfactual                             | Result                                                                                               |
| ------------------------------------------ | ---------------------------------------------------------------------------------------------------- |
| baseline                                   | `1 passed`                                                                                           |
| `main.sanitize_request_payload` → identity | **FAILED** at `:328` (`Left contains 2 more items: {'tool_choice': 'none', 'tools': []}`)            |
| `main.log_cache_stats` → no-op             | **FAILED** at `:341` (`assert [] == ['[CACHE HIT] 3840/4387 tokens (87.5%) via openrouter_backup']`) |
| `CooldownRegistry.record_failure` → no-op  | **FAILED** at `:318` (`assert 2 == 1`)                                                               |

The `== [ … ]` (not `in`) on the cache lines also proves leg A's streaming response emitted **no** cache  
line, independently corroborating the "no SSE parsing" limitation.

*Realism caveat (not a defect):* the spec narrative says "a tools schema" but the test sends the  
degenerate `"tools": []` + `"tool_choice": "none"` — the only shape in which verification 2 is  
observable, since the sanitizer deliberately preserves a **non-empty** tools list (Task 1.4 F1 fix).

**§8.2 — Cache-telemetry interpretation matches the spec's resolution.** Leg A streaming, leg B  
non-streaming; verifications 3 and 4 both derive from the follow-up. No SSE parsing added; the  
limitation is documented honestly.

**§8.3 — The `python -m gateway` gap is corrected and now accurate.** Bare invocation → exit 0;  
console script → exit 0; the subprocess test is deterministic with **or without** the editable install  
(it sets `PYTHONPATH` itself), cannot hang (`timeout=30`, measured 1.54 s), and binds no port.

**§8.4 — `egg-info` covered by `.gitignore:26` (`*.egg-info/`).** The install is mildly problematic  
(see MINOR-2); non-blocking, disclosed in the release report.

**§8.5 — `resolve_config_path` precedence correct**, all four branches tested.

**§8.6 — Banner purity confirmed**; `format_startup_banner` contains no `print`; `run_cli` prints with  
`flush=True`; `isascii()` true.

**§8.7 — No test calls `run_cli` in-process without monkeypatching `uvicorn.run`.**

## Verified independently

- `pytest -q` → `76 passed` (4 runs: 5.34 / 5.77 / 5.92 / 6.43 s). collect-only: adapters 36, config 3,  
  e2e_chaos 15, failover 13, proxy 9. `find src/gateway -name '*.py'` → 11.
- `mypy --strict src/gateway` → `Success: no issues found in 11 source files`; bare `mypy` → same.
- Bare `python -m gateway --help` (no `PYTHONPATH`) → exit 0; `sharellm-gateway --help` → exit 0;  
  `python -m gateway --help` from `C:/Users/Notebook` (outside the project) → exit 0.
- `pip show sharellm-gateway` → `Editable project location: …/sharellm-gateway`.
- **Zero-leak harness (20 runs, synchronous assertions):** `released-at-disconnect 20/20`,  
  `loop errors: []`; per run `close_count == 1` on both streams and  
  `backup.released == [BACKUP_SSE_CHUNKS[0]]` (only the TTFT-buffered first chunk read ahead).
- **Socket audit over the whole suite:** `TOTAL_BINDS 27`, `LISTENS 0`,  
  `DISTINCT_BINDS ["('127.0.0.1', 0)"]`, `NON_EPHEMERAL_BINDS []`.
- **Inertness:** `sys.addaudithook` around `import gateway.main` → no socket/open events;  
  `if __name__ == "__main__": run_cli()` present.
- **Hygiene:** no `TODO`/`FIXME`/`XXX`/`HACK`/placeholder, no bare `except`, no `sleep` in `tests/`,  
  no `aiter_text`/`aiter_lines` in `src/`.
- **Blast radius:** `test_config.py` 4859 B `fd203d6c652d9c52` (match); `test_adapters.py` 27028 B  
  (mtime 21:44:23 = the 1.4 round-2 group); `test_failover.py` mtime 21:33:50 (1.3);  
  `test_proxy.py` mtime 20:57:43 (1.2). All pre-existing test files predate the Task 1.5 edit window  
  (21:56–22:03); per-file counts unchanged from the 1.4 baseline. No existing test touched.

## Milestone 1 readiness

Ready to freeze. The codebase is internally coherent, fully typed, green, and the capstone journey is a  
real end-to-end exercise of the routing stack whose every required assertion was independently  
reproduced and proved discriminating. Three residual risks the release report should disclose:  
(1) **the freeze is not a VCS tag** — no git repository existed, so "Milestone 1" would otherwise be  
defined by the 76-green suite + `mypy --strict` gate alone (since resolved by the orchestrator);  
(2) **a bare `python -m gateway` depends on an environment-level editable install** into the shared  
`envs/default` venv (or `PYTHONPATH=src`), which a fresh checkout will not have, and which installs the  
generically-named top-level package `gateway` globally — no README documents the setup step;


(3) **streaming responses' own cache details are never logged** (deliberate, spec-mandated zero-copy,  
honestly documented). None is a code defect.

## Verdict rationale

Every Scope item and Constraint in `analysis/task-1.5.md` is met, with the single exception of the  
Objective's un-actionable "tag Milestone 1" (no VCS existed) and the spec-internal `run_cli`-in-tests  
contradiction, both documentation/process matters rather than defects. The Reviewer independently  
reproduced the F2 behaviour, the rendered ASCII banner, the bare `python -m gateway` and console-script  
invocations, the 20/20 disconnect-time release with exactly one close per stream, the 0-listen /  
27-ephemeral-bind socket profile, `mypy --strict` on 11 files, and byte-identity of `test_config.py`;  
and proved by counterfactual that verifications 2, 3 and 4 of the chaos journey genuinely fail when  
their feature is disabled. Two MINOR findings and six NITs remain, none blocking ⇒ **APPROVED**.
