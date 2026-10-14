# Review — Task 1.4 (Round 2)

STATUS: APPROVED

Reviewer: independent read-only review (fresh agent instance, no prior context).
Note: the configured Reviewer model `Atria-Dawn-Preview` was unavailable (429 token quota
exhausted), so this round ran on the `hy4-preview-f` model with the Reviewer agent's read-only
tool restrictions intact. No VCS baseline exists (no `.git`, no `.orig`/`.bak`), so byte-level diffs
of pre-existing files are unavailable — the Reviewer used mtimes plus content inspection and said so.

## Round-1 delta adjudication

| Item | Verdict | Evidence |
|---|---|---|
| **F1** (MAJOR) `tool_choice` over-strip | **FIXED** | `sanitizer.py:79-83` gates the strip on `not _has_declared_tools(sanitized)` (helper `:32-35`). Probe: non-empty `tools` + `tool_choice` `"none"`/`None`/`"auto"`/`"required"`/object → **all preserved**, `same_obj=True`; `tools: []`/absent + `"none"`/`None` → both stripped; no-tool payload → **same object** |
| **F3** (MINOR) missing test | **FIXED (discrimination proven)** | `test_adapters.py:471-496`. Re-ran with the pre-fix algorithm monkey-patched in: `FIXED rc: 0  PRE-FIX rc: 1  discriminating: True` — pre-fix raises `KeyError: 'tool_choice'` at `:495`. Genuinely discriminating, not a tautology |
| **F4** (NIT) handoff wording | **FIXED** | `builder_handoff_1_4.md:349` now reads "No *pre-existing* test imports or calls `dispatch`". `grep -rn dispatch tests` → only `test_adapters.py:40,169,193` |
| **F5** (MINOR) typing unverifiable | **FIXED** | mypy clean three ways. The fix **narrows** rather than widens: `_read_error_body` still declares `-> JSONValue` (`proxy.py:196`); only the body binds `payload: JSONValue = response.json()` (`:211`). Counterfactual via `mypy --strict -c`: pre-fix body → `[no-any-return]`; fixed body → `Success` |
| **F6** (NIT) assert identity | **FIXED** | `test_adapters.py:535-536`: `assert sanitized is payload` **and** `assert sanitize_request_payload(payload) is payload` |
| **F2** (MINOR) unanchored markers | **ACCEPTED AS LIMITATION** | Genuinely unchanged: `sharellm.py` mtime `21:33:00` (round 1), not in the round-2 `21:44` edit group; markers still `:57`, match at `:106`. Honestly documented in the handoff |

## Spec compliance

| Requirement | Result | Evidence |
|---|---|---|
| §1 per-target model rewrite on a **copy** | **MET** | `router.py:260` `target_payload = {**request_data, "model": target.model}` inside the per-attempt loop; caller dict unmutated |
| §2a `sanitize_error_envelope` + `message`/`code` preservation | **MET** | `sharellm.py:115-153`; guard `:139`, mapping `:143-145`, non-mutating copy `:151-153` |
| §2b `extract_cache_stats` ratio | **MET** | `sharellm.py:156-198`; `:193` `round(cached/prompt*100, 1)`. `3840/4387 → 87.5`; malformed → `None` |
| §3 anti-422 parameter sanitizer | **MET** (was PARTIAL) | `sanitizer.py:70-90`; the F1 gate at `:79` removes the round-1 defect |
| §4 `main.py` integration | **MET** | sanitize→dispatch `main.py:240`; streaming error normalize `:257`; buffered `:274`; cache log non-streaming-only `:278-281` |
| §5 four required adapter tests | **MET** | rewrite `test_adapters.py:135`, 429→rate_limit `:222`, cache ratio `:337`, empty-tool sanitize `:427` |
| Constraint: backward compat | **MET** | `61 passed`; per-file 3/9/13/36; `test_config.py` byte-identical |
| Constraint: zero leaks | **MET** | 20/20 release-at-disconnect |
| Constraint: fully typed / mypy | **MET (now verified)** | clean under `mypy`, `mypy src/gateway`, `mypy --strict src/gateway` |

## Findings

No BLOCKER and no MAJOR findings. Two NITs, both outside the task's scope and non-blocking:

1. **NIT — `sanitizer.py:79-83`.** With a **non-empty `tools`**, a `tool_choice: null` is now
   preserved. The spec's framing is "unsupported **null**/empty tool fields", so `null` is arguably
   still a field the guard exists to clean. This is the conservative choice and exactly what the
   round-1 recommendation prescribed ("strip only when `tools` is absent or empty"), and no 422 on
   this shape is demonstrated — acceptable, noted for awareness. "Fixed" would gate only the `"none"`
   string on `tools`-empty while still stripping a bare `None`. Not required.
2. **NIT — `sanitizer.py:32-35,79`.** `_has_declared_tools` counts only a non-empty **list** as
   declared tools, so a malformed non-list `tools` (e.g. a string) leaves `tool_choice: "none"`
   stripped while `tools` survives. Probe: `{"tools":"foo","tool_choice":"none"}` → `{"tools":"foo"}`.
   Malformed input the spec never mentions; harmless.

## Verified independently

- **Suite:** `61 passed in 3.85s`; `--collect-only` → 3 config + 9 proxy + 13 failover + 36 adapters.
- **mypy (real output):** `mypy` → `Success: no issues found in 10 source files`; `mypy src/gateway` →
  same; `mypy --strict src/gateway` → same. `find src/gateway -name '*.py'` = 10 files, so
  `[tool.mypy] files=["src/gateway"]` is correctly scoped and does **not** sweep `tests/`.
- **F5 narrowing counterfactual** (`mypy --strict --config-file=/dev/null -c`): pre-fix body
  `error: Returning Any … [no-any-return]`; fixed body `Success`. Declared return type unchanged.
- **F3 discrimination:** fixed vs re-implemented pre-fix sanitizer → `FIXED rc: 0  PRE-FIX rc: 1
  discriminating: True` (`KeyError` at `:495`).
- **E2E (own ASGITransport+respx probe):** buffered failover 429→200 (`primary=1, backup=1`, backup
  body model `gpt-6-astra`); streaming 502→SSE **byte-exact** (`text/event-stream`, primary released,
  only first chunk read ahead); timeout→200; all-429 → 429 `gateway_failover_exhausted`; cooldown skip
  (`primary=1, backup=2`); cache line `[CACHE HIT] 3840/4387 tokens (87.5%) via sharellm_primary` for
  non-streaming, **absent** for streaming; buffered 400 `share_llm_error` → `invalid_request_error`
  (`code` kept) and **streaming** 400 → `invalid_request_error`; `dispatch` caller dict unmutated.
- **Zero-leak harness (20 runs):** `both released synchronously at disconnect: 20/20`; primary exactly
  one close 20/20; backup exactly one close 20/20; only first chunk read ahead 20/20; loop exception
  events `[]`. Assertions taken synchronously with no `gc`/await.
- **Ports:** audit hook over the whole suite in-process → `socket.listen: 0`, 24 `socket.bind` all
  ephemeral `('127.0.0.1', 0)`.
- **Inertness:** `sys.addaudithook` around `import gateway.main` → no socket/open events; `run_cli`
  only reachable under `if __name__ == "__main__"` (`main.py:308-309`).
- **Hygiene:** no `TODO`/`FIXME`/`XXX`/placeholder; the only `mock` matches are legitimate
  `respx.mock(...)`; no bare `except`/`except Exception`.
- **Backward-compat evidence:** `test_config.py` = 4859 B, SHA-256
  `fd203d6c652d9c523c2f6840e7e45bd9e08cf347aad2e548ed5152b77c1a2f90` (matches). mtimes:
  `config.example.yaml` 19:10, `config.py` 19:18, `test_proxy.py` 20:57 — all predate the 1.4 edits;
  round 2 (`21:44`) touched only `sanitizer.py`, `proxy.py`, `test_adapters.py`, `pyproject.toml`,
  leaving `sharellm.py` (F2) and `test_failover.py` untouched.

## Verdict rationale

The Round-1 MAJOR (F1) is genuinely fixed — the strip is now gated on the absence of a non-empty
`tools`, the four expected behaviours all hold under the Reviewer's own probes, and the new F3 test is
demonstrably discriminating (fails against the pre-fix algorithm). F4/F5/F6 are addressed, mypy is
clean three ways with a real narrowing (proven by counterfactual), F2 is honestly left unchanged and
spec-literal, the suite is 61 green, the zero-leak invariant holds 20/20 at disconnect time, and no
real ports are bound. Only two non-blocking NITs remain, both on malformed/out-of-scope input;
nothing rises to MAJOR or BLOCKER ⇒ **APPROVED**.
