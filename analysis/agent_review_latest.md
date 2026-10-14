# Review — Task 1.5 (Round 2)

STATUS: APPROVED

Reviewer: independent read-only review (fresh agent instance, no prior context).
Note: the configured Reviewer model `Atria-Dawn-Preview` was unavailable (429 token quota
exhausted), so this round ran on the `hy4-preview-f` model with the Reviewer agent's read-only
tool restrictions intact. All probes ran from a temp directory; nothing was written into the tree.

## Round-1 delta adjudication

**NIT-6 — `engine/__init__.py` docstring — FIXED.** `src/gateway/engine/__init__.py:1-15` now
describes the two modules, and every claim is true against the code: "strips hop-by-hop headers" →
`proxy.py:59-71` applied at `:140-145`; "buffered JSON response **or** a raw byte stream whose first
chunk is inspected (TTFT) before the caller's stream commits" → `proxy.py:180-193` and `:257-288`
(first chunk pulled at `:280`); "walks a route's ordered targets, treats retryable statuses (429/5xx)
and transport errors as failures" → `router.py:57`, `:274`; "skips recently failed targets through a
per-application `CooldownRegistry`" → `router.py:88-131`, `:134-145`. It neither over-claims nor omits
anything material. The `adapters/__init__.py` audit is correct (names exactly the two modules that
exist); `utils/__init__.py:5` ("intentionally empty") is still literally true. Both correctly left
alone.

**NIT-1 — `_program_name()` — FIXED.** Real invocations, not just the unit test:

```
$ sharellm-gateway --help                      → usage: sharellm-gateway [-h] [--config PATH]   exit=0
$ PYTHONPATH=src python -m gateway --help      → usage: gateway [-h] [--config PATH]            exit=0
```

The Reviewer also drove the **actual `.exe` launcher** through an argparse error to capture the real
`sys.argv[0]` (`sharellm-gateway --bogus` → `usage: sharellm-gateway …`, exit 2; `python -m gateway
--bogus` → `usage: gateway …`, exit 2), and probed `main.py:317-340` directly:
`…/sharellm-gateway.exe`→`sharellm-gateway`, `…/SHARELLM-GATEWAY.EXE`→`SHARELLM-GATEWAY`,
`…/gateway/__main__.py`→`gateway`, `""`/`-m`/`-c`/`__main__`→`gateway`. Fallback rule is sane.

**NIT-3 — banner purity test — FIXED (proven discriminating).** `test_e2e_chaos.py:692-723` asserts
`captured.out == ""` and `captured.err == ""`. The Reviewer injected a runtime `print` into
`format_startup_banner` via a pytest plugin (no file edited): control run **passed**, injected run
**FAILED** at `:722` (`assert 'INJECTED_PRI...T_TO_STDOUT\n' == ''`). The test genuinely catches
printing.

**NIT-2 — chaos docstring / derived URL — FIXED.** `test_e2e_chaos.py:85-87` defines
`EXPECTED_BASE_URL` from the loaded config's host/port, used at `:706-708` and `:747-748`. No literal
`127.0.0.1:8000` remains in assertions. The docstring (`:17-24`) is accurate and explicitly carves out
the three legitimate literals (`MODEL_ALIAS`, `GATEWAY_BASE_URL`, cross-check ids).

**Deliberately unchanged:** NIT-4, NIT-5, MINOR-1 (no git repo — confirmed absent in root, parent and
grandparent), MINOR-2 (editable install). Verified by mtime: only `main.py` (22:13:41),
`engine/__init__.py` (22:13:35) and `test_e2e_chaos.py` (22:14:05) were touched this round;
`adapters/__init__.py` (21:33:11) and `utils/__init__.py` (19:10:03) untouched.

## Spec compliance

| Requirement | Verdict | Evidence |
|---|---|---|
| §1 F2 word-boundary, case-insensitive | MET | `sharellm.py:60`; `400 "unlimited tokens available"`→`invalid_request_error`, `"limitless"`/`"overloadedness"`→`invalid_request_error`, `"rate limit exceeded"`/`"limits"`/`"pre-limit"`/`"LIMIT"`→`rate_limit_error` |
| §2 `__main__.py`; `python -m gateway --help` without binding | MET | `__main__.py:14-17`; real exit 0 |
| §2 `--config` → `config.yaml` → `config.example.yaml` fallback | MET | `main.py:380-421`; all 4 branches tested |
| §2 banner: URL, routes + fallback targets, Cursor/Cline tip, ASCII | MET | `main.py:424-469`; rendered banner verified, `isascii True` |
| §3 chaos test proves all 4 verifications, genuinely E2E | MET | `test_e2e_chaos.py:234-361`; independently reproduced |
| §4 suite green, `mypy --strict`, zero socket leaks | MET | 81 passed; `Success: no issues found in 11 source files`; 20/20 disconnect-time release |
| Cache-telemetry resolution (leg A streaming / leg B non-streaming, **no SSE parsing**) | MET | `test_e2e_chaos.py:287-303`; no `aiter_text`/`aiter_lines` in `src/` |
| Testability — pure `parse_args`/`resolve_config_path`/`format_startup_banner` | MET | `main.py:343/380/424`; all directly tested |
| Back-compat: 61 pre-existing green; `test_config.py` untouched | MET | 36+3+20+13+9 = 81; `test_config.py` 4859 B `fd203d6c652d9c52` |
| Deterministic, zero-network-bound | MET | audit: 27 binds all `('127.0.0.1', 0)`, 0 listens, no fixed port |
| ASCII-only banner | MET | `banner.isascii() is True` |

## Findings

No BLOCKER, no MAJOR, no new MINOR.

1. **NIT — `main.py:334-340`** — `_program_name`'s fallback is incomplete for a direct-script
   invocation: `python src/gateway/main.py` → `usage: main.py`; `python.exe` → `python`. Defensible
   (it reports how it was invoked) and both spec-required invocations are correct. Cosmetic only.
2. **NIT (residual, accepted)** — `test_e2e_chaos.py:791` `assert elapsed < 15.0` is a wall-clock
   bound; measured ≈1.3-1.5 s (~10× margin). Deliberately left.
3. **Observation (process, not a defect)** — MINOR-1 / MINOR-2 carry forward as release-report
   disclosures.

## Verified independently

- **Suite:** `81 passed in 5.37s` (also 81 passed in 4.24 s under the audit hook). Per-file:
  adapters 36, config 3, e2e_chaos 20, failover 13, proxy 9.
- **mypy:** `--strict src/gateway` and bare `mypy` → `Success: no issues found in 11 source files`.
- **CLI:** real `sharellm-gateway --help` and `python -m gateway --help` both exit 0 with the correct
  `usage:` program name, plus real-`.exe` and `-m` error-path proofs.
- **Banner:** rendered verbatim, `isascii True`, tip == gateway URL.
- **Chaos journey (own harness):** `status 200`, `content-type text/event-stream; charset=utf-8`,
  byte-exact SSE `True`; target-1 body kept `gpt-6.1-sol`; target-2 leg-A body
  `{'model':'gpt-6-astra','messages':<intact>,'stream':True}` with keys `['messages','model','stream']`
  (tool fields stripped); cache line `['[CACHE HIT] 3840/4387 tokens (87.5%) via openrouter_backup']`;
  primary cooling `True`, backup `False`; calls `primary 1 / backup 2`; follow-up 200 body match.
- **F2:** 16-case matrix as above.
- **Zero-leak (20 runs):** `released-at-disconnect 20/20`, `loop errors: []`; each run
  `primary.close_count == 1`, `backup.close_count == 1`, `backup.released == [first_chunk]` (only the
  TTFT chunk read ahead).
- **Socket audit over the whole suite:** `TOTAL_BINDS 27`, `LISTENS 0`,
  `DISTINCT_BIND_ADDRS ["('127.0.0.1', 0)"]`, `NON_EPHEMERAL_BINDS []`.
- **Inertness:** `sys.addaudithook` around `import gateway.main` → `socket events: []`;
  `if __name__ == "__main__": run_cli()` present (`main.py:495-496`).
- **Hygiene:** no `TODO`/`FIXME`/`XXX`/`HACK`/placeholder/`NotImplemented`, no bare `except`, no
  `except Exception`, no `sleep`, no `mock`/`patch` in `src/`, no SSE body parsing.
- **Blast radius:** `test_config.py` 4859 B `fd203d6c652d9c52`; `test_adapters.py` 21:44:23,
  `test_failover.py` 21:33:50, `test_proxy.py` 20:57:43 all predate the Task 1.5 window.

## Milestone 1 release readiness

The frozen artifact is internally coherent, fully typed, green, and the capstone journey is a genuine
end-to-end exercise whose every required assertion was reproduced independently. The release report
must disclose: (1) **the freeze is not a VCS tag** — no git repository exists, so "Milestone 1" is
currently defined only by the 81-green suite + `mypy --strict` gate (the orchestrator tags it); (2) **a
bare `python -m gateway` depends on an environment-level editable install** into the shared
`envs/default` venv (or `PYTHONPATH=src`) — a fresh checkout lacks it, and the install exposes the
generically-named top-level package `gateway` globally; no README documents the setup step; (3)
**streaming responses' own cache details are never logged** (deliberate, spec-mandated zero-copy
streaming); (4) **verification 2 is demonstrated only for the degenerate `tools: []` +
`tool_choice: "none"` shape** — a non-empty tools list is deliberately preserved (covered at unit
level by `test_adapters.py`, not through the chaos path); (5) the `uvicorn.run` *serve* path (actual
port bind) is intentionally never exercised; (6) NIT-4's wall-clock bound is environment-sensitive.
None is a code defect.

## Verdict rationale

All four round-1 deltas are genuinely fixed, each confirmed with evidence the Reviewer produced itself
— including a runtime `print`-injection proving the banner purity test discriminates, and the real
`.exe` launcher proving the `usage:` program name. The full suite is green (81 passed), `mypy --strict`
is clean on 11 files, no fixed port is bound, the disconnect harness releases every stream exactly once
at disconnect time (20/20), hygiene is clean, and no pre-existing test was touched. With no BLOCKER and
no MAJOR findings ⇒ **APPROVED**.
