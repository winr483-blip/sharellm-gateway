# Evidence Package — Task 1.4 (Review Round 1)

## 1. Task specification

`analysis/task-1.4.md` — "Task 1.4 — Target Model Mapping, ShareLLM Adapter & Telemetry".
Read it in full, including the section "Known blast radius of the model-rewrite contract change".

## 2. Project root

    C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway

## 3. Builder handoff

`analysis/builder_handoff_1_4.md` — STATUS: COMPLETE.

## 4. Files created / changed

    src/gateway/adapters/sharellm.py     (created)  sanitize_error_envelope, extract_cache_stats, format/log_cache_stats
    src/gateway/adapters/sanitizer.py    (created)  sanitize_request_payload (anti-422 guard)
    tests/test_adapters.py               (created)  35 tests
    src/gateway/engine/router.py         (modified) per-target model rewrite; dispatch now returns (result, provider_name)
    src/gateway/main.py                  (modified) sanitize -> dispatch -> normalize error -> cache log
    src/gateway/adapters/__init__.py     (modified) docstring only
    tests/test_failover.py               (modified) ONLY lines 168-180 (comment + backup assertion)

`tests/test_config.py`, `tests/test_proxy.py`, `src/gateway/config.py`, `src/gateway/engine/proxy.py`,
`config.example.yaml`, `pyproject.toml` — untouched. No git repo exists; inspect files directly.

## 5. Reviewer reading list (priority order)

1. `src/gateway/adapters/sharellm.py`
2. `src/gateway/adapters/sanitizer.py`
3. `src/gateway/engine/router.py` (the rewrite + the new `provider_name` return)
4. `src/gateway/main.py` (the integration)
5. `tests/test_adapters.py`
6. `tests/test_failover.py:168-180`
7. `analysis/task-1.4.md`

## 6. Build / test output

    cd "C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway" && \
      C:/Users/Notebook/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe -m pytest -q

    60 passed in 3.42s

Composition: 3 config + 9 proxy + 13 failover + 35 adapter. `tests/test_config.py` independently
re-hashed by the orchestrator: SHA-256 prefix `fd203d6c652d9c52`, 4859 bytes — unchanged.

## 7. Orchestrator's independent probe

    targets: primary.model='gpt-6.1-sol'  backup.model='gpt-6-astra'

    -- sanitize_error_envelope --
      429 share_llm_error        -> rate_limit_error       | msg preserved=True code='c1'
      400 share_llm_error        -> invalid_request_error  | msg preserved=True code='c1'
      503 share_llm_error        -> api_error              | msg preserved=True code='c1'
      400 share_llm_error        -> rate_limit_error       | msg preserved=True code='c1'   (message contained "limit")
      401 share_llm_error        -> share_llm_error        | msg preserved=True code='c1'   (unmapped status, left alone)
      429 invalid_request_error  -> invalid_request_error  | msg preserved=True code='c1'   (non-share_llm, untouched)
      malformed (no error key): {'foo': 1}
      non-dict error         : {'error': 'nope'}

    -- extract_cache_stats --
      3840/4387 -> {'cached_tokens': 3840, 'prompt_tokens': 4387, 'cache_hit_pct': 87.5}
      zero prompt -> None | no usage -> None | bool tokens -> None
      log line    -> [CACHE HIT] 3840/4387 tokens (87.5%) via sharellm_primary

    -- sanitize_request_payload --
      no-tools unchanged (same obj): True
      tools:[] removed       : True
      tool_choice 'none' only: True
      tools non-empty KEPT   : True
      tool_choice 'auto' KEPT: True
      stream_options non-dict: True
      stream_options not streaming: True
      stream_options while streaming KEPT: True
      NUANCE tools+tool_choice='none' -> {'tools': [{'type': 'function'}]} | tool_choice preserved: False

    -- E2E model rewrite --
      status=200 primary.model='gpt-6.1-sol' backup.model='gpt-6-astra'
      caller dict unmutated: True

    -- E2E cache telemetry --
      logged: ['[CACHE HIT] 3840/4387 tokens (87.5%) via sharellm_primary']

    -- E2E error sanitization (non-retryable 400) --
      status=400 body={"error": {"type": "invalid_request_error", "message": "bad model", "code": "m1"}}

    -- E2E streaming error sanitization (non-retryable 400) --
      status=400 body={"error": {"type": "invalid_request_error", "message": "bad", "code": null}}

    -- E2E exhausted envelope untouched --
      status=429 body={"error": {"message": "All upstream targets failed", "type": "gateway_failover_exhausted"}}

## 8. Specific items to adjudicate

1. **`tool_choice: "none"` with non-empty `tools` — the orchestrator's main concern.**
   The probe's NUANCE line shows: a payload with `tools: [<definitions>]` **and**
   `tool_choice: "none"` comes back with `tools` **preserved** but `tool_choice` **removed**. The
   client's explicit instruction "here are tools, but do not call them" is therefore dropped while
   the tool definitions remain, so the upstream falls back to its default (`auto`) and **may call a
   tool the client explicitly disabled**. The spec lists `tool_choice: "none"` as a field to clean,
   but its framing is "unsupported **null or empty** tool fields" — with non-empty `tools`,
   `tool_choice: "none"` is neither null nor empty and carries real intent.
   **Judge whether this is a MAJOR correctness issue (silently changing caller intent) or an
   acceptable reading of the spec.** The safe rule would be: only strip `tool_choice` when `tools`
   is absent or empty, i.e. when it is genuinely a no-op.

2. **`dispatch` now returns `(result, provider_name)`** — a change beyond the stated blast radius.
   Justification: the cache log must name the provider, and only `dispatch`'s loop knows which
   provider actually served after a failover. The Builder states no test calls `dispatch` directly.
   Verify that claim and judge whether the signature change is justified or whether the provider
   should be threaded differently.

3. **The cache-log signature interpretation.** The spec pins
   `extract_cache_stats(response_json: dict) -> dict | None` *and* says it "emits a structured log …
   `via <provider>`" — impossible from `response_json` alone. The Builder made the extractor **pure**
   and logs at the integration point. Judge whether that is the right resolution and whether the
   emitted line matches the spec's format.

4. **`sanitize_error_envelope` on unmapped statuses.** A `share_llm_error` on 401/403/404/422 is
   returned **unchanged** (type stays `share_llm_error`). The spec only covers 429/400/5xx. Judge
   whether leaving it alone is correct or whether it should be mapped to `api_error`.

5. **Precedence.** A 400 whose message contains "limit" maps to `rate_limit_error`, not
   `invalid_request_error`. Confirm this matches the spec's stated order.

6. **Exhausted envelope is not sanitized** (its type is `gateway_failover_exhausted`, not
   `share_llm_error`, so `sanitize_error_envelope` returns it unchanged). Confirm that is right.

## 9. Reviewer instruction

Verify against `analysis/task-1.4.md` yourself. Do not take the Builder's claims or the
orchestrator's probe on trust — re-read the files, write your own probes, and re-run the suite.
Report a structured verdict.
