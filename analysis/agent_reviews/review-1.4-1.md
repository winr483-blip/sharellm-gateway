# Review — Task 1.4 (Round 1)

STATUS: **CHANGES_REQUIRED**

Reviewer: independent read-only review (fresh agent instance, no prior context).
Note: the configured Reviewer model `Atria-Dawn-Preview` was unavailable (429 token quota
exhausted), so this round ran on the `hy4-preview-f` model with the Reviewer agent's read-only
tool restrictions intact.

## Spec compliance

| Requirement | Result | Evidence |
|---|---|---|
| §1 target model rewriting, per-target on a **copy** | **MET** | `router.py:260` `target_payload = {**request_data, "model": target.model}` inside the per-attempt loop; caller dict unmutated (probe) |
| §2a `sanitize_error_envelope` mapping | **MET** | `sharellm.py:115-153`; 429→`rate_limit_error`, 400→`invalid_request_error`, 5xx→`api_error`; `message`/`code` preserved; input not mutated |
| §2b `extract_cache_stats` | **MET** | `sharellm.py:156-198`; `3840/4387 → cache_hit_pct 87.5` |
| §3 parameter sanitizer (anti-422) | **PARTIAL** | `sanitizer.py:54-71` removes the targeted fields, but strips `tool_choice: "none"` even when `tools` is non-empty — see **F1 (MAJOR)** |
| §4 `main.py` integration | **MET** | `main.py:240` sanitize → dispatch; `:257`/`:274` normalize errors on both paths; `:278-281` cache log for non-streaming only |
| §5 four required tests | **MET** | rewriting (`test_adapters.py:134`), 429→rate_limit (`:222`), cache ratio (`:337`), empty-tool sanitize (`:427`) |
| Constraints: backward compat | **MET** | 60 passed; only the backup assertion in `test_failover.py` changed |
| Constraints: zero leaks | **MET** | Disconnect tests pass; release at disconnect time |
| Constraints: fully typed / mypy | **UNVERIFIED at review time** | `mypy` was not installed. **The orchestrator has since installed `mypy` 2.4.0 + `types-PyYAML`** and measured it: `mypy src/gateway` → **clean**; `mypy --strict src/gateway` → 1 error at `proxy.py:208` (`Returning Any ... [no-any-return]`) |

**Blast radius verified:** `tests/test_config.py` byte-identical (4859 B, `fd203d6c652d9c52`);
`tests/test_proxy.py` unchanged and passing (its only body assertions target the primary);
`tests/test_failover.py` changed only at 168-181.

## Findings

**F1 — MAJOR — `src/gateway/adapters/sanitizer.py:60-64`**
`tool_choice: "none"` is stripped unconditionally. When the payload also carries a **non-empty
`tools`** list, the tool definitions survive but the explicit "do not call tools" instruction is
dropped, so the upstream falls back to its default (`auto`) and may call a tool the client explicitly
disabled. This silently changes caller intent — the exact failure mode a transparent gateway must
avoid.

```
NON-EMPTY tools + tool_choice none
   in : {"model":"m","messages":[],"tools":[{"type":"function",...}],"tool_choice":"none"}
   out: {"model":"m","messages":[],"tools":[{"type":"function",...}]}   # tool_choice dropped, tools kept
```

The spec frames the category as "unsupported **null or empty** tool fields"; with non-empty `tools`,
`tool_choice: "none"` is neither null nor empty and carries real intent. The Builder's own docstring
claims it removes fields that "carr[y] no user intent" (`sanitizer.py:9-10`), which this case
violates. *Fixed:* strip `tool_choice` only when it is a no-op — i.e. only when `"tools"` is absent
or empty.

**F2 — MINOR (spec-compliant, robustness note only)** — `sharellm.py:57,106`. The rate-limit markers
are unanchored substrings, so `400 share_llm_error "unlimited access granted" → rate_limit_error`.
This is literally what the spec asks ("the message contains `"limit"`"), so it is compliant; flagged
for awareness. **Deliberately not changed** — see Adjudications.

**F3 — MINOR — `tests/test_adapters.py` coverage gap.** The risky combination (non-empty `tools` +
`tool_choice: "none"`) — the exact subject of F1 — is untested. *Fixed in round 2.*

**F4 — NIT — handoff imprecision** (`builder_handoff_1_4.md:349`). "No test calls `dispatch`
directly" is imprecise: the new `tests/test_adapters.py:193` does. The material claim holds — no
**pre-existing** test imports or calls `dispatch`, so the signature change required zero edits to
existing tests.

**F5 — MINOR — typing unverified.** `mypy` was not installed, so "fully typed / mypy compliant" was
an assertion. Manual inspection found no violations. **Resolved post-review by the orchestrator**
(see table above) and actioned in round 2.

**F6 — NIT — `tests/test_adapters.py:491-504`** asserts value and `json.dumps` equality but not
object identity, while the handoff claims `sanitized is payload`. Identity was verified independently
(`same_obj=True`); the test could assert `is` to lock the guarantee.

No BLOCKER-level issues.

## Adjudications

1. **`tool_choice: "none"` with non-empty `tools` → MAJOR.** The Reviewer wrote its own probe and
   confirmed the behaviour. Judged a **real correctness bug**, not an acceptable reading of the spec:
   the spec's category is "null/empty tool fields" and this case is neither. The safe rule (strip
   only when `tools` is absent/empty) is strictly better and still satisfies the spec's goal.
   → CHANGES_REQUIRED.
2. **`dispatch` returns `(result, provider_name)` → justified.** Only `main.py` and the new
   `test_adapters.py:193` call it; no pre-existing test does, so the blast-radius claim holds. After
   failover the serving provider is genuinely unknown to `main.py`, so returning it from the loop is
   the correct, side-effect-free way to attribute the cache line.
3. **Cache-log signature split → correct resolution.** `extract_cache_stats` is pure; the
   `[CACHE HIT] … via <provider>` line is emitted at the integration point where the provider is
   known. The spec's signature and its `via <provider>` line cannot both come from `response_json`;
   splitting is the only coherent reading. The emitted line matches the spec format byte-for-byte.
4. **Unmapped statuses (401/403/404/422) left unchanged → correct.** The spec covers only 429/400/5xx;
   leaving the upstream `type` untouched rather than inventing `api_error` is the conservative call.
5. **Precedence (400 + "limit" → `rate_limit_error`) → matches the spec's stated order** (rate-limit
   listed first).
6. **Exhausted envelope not sanitized → correct.** Its `type` is `gateway_failover_exhausted`
   (non-`share_llm_error`), so it is returned untouched and the failover tests pinning it stay green.

## Verified independently

- **Suite:** `60 passed in 3.57s` (3 config + 9 proxy + 13 failover + 35 adapter).
- **`sanitize_error_envelope`:** 16-case probe — correct mapping, `message`/`code` preserved,
  unmapped/non-ShareLLM/non-dict/missing-`error`/missing-`type` all return **by identity**, input not
  mutated, top-level extras preserved; nothing raised.
- **`extract_cache_stats`:** 13-case probe — missing/non-dict `usage`, `prompt_tokens == 0`,
  negative/non-numeric/`bool` `cached_tokens`, `bool` `prompt_tokens`, missing
  `prompt_tokens_details` all return `None`; `3840/4387 → 87.5`; `1/3 → 33.3`; nothing raised.
- **Sanitizer:** 13-case probe — no-tool payload returned as the **same object**; `tools: []`/`None`,
  `tool_choice: "none"`/`None`, bad/non-streaming `stream_options` removed; non-empty `tools`,
  `"auto"`/`"required"`/object `tool_choice`, unrelated fields, `model` all survive. **F1 reproduced.**
- **E2E:** non-streaming `400 share_llm_error` → `invalid_request_error`; **streaming** `400
  share_llm_error` → `invalid_request_error`; failover bodies `gpt-6.1-sol`/`gpt-6-astra`; caller dict
  unmutated; cache line logged non-streaming, absent for streaming.
- **Inert import:** `import gateway.main` under an audit hook → no `socket.bind/listen/connect`, no
  `config.yaml` read. `run_cli` guarded.
- **Ports:** whole suite under `sys.addaudithook` → **0 `socket.listen`**; 24 binds, all
  `('127.0.0.1', 0)`. No real ports bound.
- **Disconnect:** both zero-leak tests re-run → 2 passed; release asserted synchronously at disconnect.
- **Hashes/mtimes:** `test_config.py` 4859 B `fd203d6c652d9c52`; `test_proxy.py` (20:57) and
  `proxy.py` (20:55) predate the 1.4 edits (21:33-21:34); `config.example.yaml`, `pyproject.toml`
  untouched.
- **Hygiene:** no `sleep`, `TODO`/`FIXME`, placeholder, or bare `except` in `src/`.

## Verdict rationale

The implementation is clean, well-typed, well-documented, and satisfies every explicit spec item
except one: the sanitizer strips `tool_choice: "none"` even when `tools` is non-empty, silently
enabling tool calls the client disabled (**F1, MAJOR**). Because that is a real, probe-demonstrated
correctness bug in the new code — with a trivial, strictly-safer fix — the verdict is
**CHANGES_REQUIRED**. Everything else (model rewrite on a copy, error normalization, cache telemetry,
the `dispatch` signature change, blast radius, zero-leak and failover invariants) checks out and
should not be re-worked.
