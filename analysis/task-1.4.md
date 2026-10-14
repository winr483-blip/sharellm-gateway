# Task 1.4 — Target Model Mapping, ShareLLM Adapter & Telemetry

Project: **ShareLLM Smart Gateway** (`sharellm-gateway`)
Project root: `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway`

Task 1.3 (Resilient Failover Engine) is verified and merged — 25 tests passing.

This task resolves the `target.model` design gap escalated at the end of Task 1.3, and adds
ShareLLM-specific error normalization, cache telemetry and a parameter sanitizer.

## Objective

1. Dynamic target model rewriting.
2. ShareLLM custom error sanitization (normalize `type: "share_llm_error"` to standard OpenAI types).
3. Prompt-caching telemetry logger (extract `cached_tokens` ratio).
4. Parameter sanitizer (anti-422 guard for incompatible upstream params).

## Scope of work

### 1. Target model rewriting (engine level)

In `src/gateway/engine/router.py`:

- Before forwarding a request to `target`, **copy** the incoming JSON payload and rewrite
  `payload["model"] = target.model`.
- This ensures that if the client requests alias `gpt-6.1-sol` and the route fails over to a backup
  declaring `gpt-6-astra` or `deepseek-chat`, the upstream receives its exact expected model id.
- The caller's own dict must not be mutated (the rewrite is per-target).

### 2. `src/gateway/adapters/sharellm.py` (new)

**`sanitize_error_envelope(status_code: int, error_json: dict) -> dict`**

- ShareLLM upstream returns errors with `type: "share_llm_error"`.
- When `type == "share_llm_error"`:
  - → `"rate_limit_error"` if status is 429 **or** the message contains `"limit"` / `"overloaded"`;
  - → `"invalid_request_error"` if status is 400;
  - → `"api_error"` for 5xx.
- Preserves the `message` and `code` fields.

**`extract_cache_stats(response_json: dict) -> dict | None`**

- Inspects `usage.prompt_tokens_details.cached_tokens` and `usage.prompt_tokens`.
- Computes the cache hit percentage: `(cached_tokens / prompt_tokens) * 100`.
- Emits a structured log, e.g. `[CACHE HIT] 3840/4387 tokens (87.5%) via <provider>`.

### 3. Parameter sanitizer (anti-422 guard)

In `src/gateway/adapters/base.py` or `sanitizer.py`:

- Clean the payload of unsupported null/empty tool fields — e.g. `tools: []`,
  `tool_choice: "none"`, or an invalid `stream_options` — that cause 422 Unprocessable Entity on
  strict upstreams.

### 4. Integration in `src/gateway/main.py`

- Plug the model rewriter and the sanitizers into the request/response lifecycle.
- When an upstream returns an error, or when failover is exhausted, normalize the error envelope
  before returning it to the client.
- For non-streaming responses, extract and log cache statistics.

### 5. `tests/test_adapters.py` (new)

- **Test 1:** target model rewriting — an inbound request for alias `alias-model` is dispatched with
  `target.model` in the upstream body.
- **Test 2:** `share_llm_error` with status 429 is sanitized to `rate_limit_error`.
- **Test 3:** the cache telemetry extractor correctly computes the ratio from
  `usage.prompt_tokens_details`.
- **Test 4:** 422-triggering empty tool schemas are safely sanitized.

## Constraints & coding standards

- **Backward compatibility:** all 25 previous tests must remain green, adjusting assertions **only**
  where the `target.model` rewriting intentionally changes the expected outbound body.
- **Zero leaks**, fully typed (`mypy` compliant).

## Known blast radius of the model-rewrite contract change (verified)

The orchestrator has already determined exactly which existing assertions are affected, so this
should be a small, surgical change:

- `PRIMARY_TARGET.model == "gpt-6.1-sol"` and the test constant `MODEL == "gpt-6.1-sol"` are
  **identical**, so rewriting `model` to `target.model` is a **no-op** on the primary target.
- `BACKUP_TARGET.model == "gpt-6-astra"` ≠ `MODEL`, so rewriting **does** change the outbound body
  for the backup target.
- Therefore:
  - `tests/test_failover.py:170-180` (inside `test_buffered_failover_on_retryable_status`) — the
    comment claiming verbatim forwarding, and the **backup** assertion at `:177-180`, must be
    updated to expect `{"model": "gpt-6-astra", ...}`. The **primary** assertion at `:173-176` stays
    valid as-is.
  - `tests/test_proxy.py` — contains **no** backup assertions; its two
    `json.loads(upstream_request.content) == inbound_payload` checks (`:217`, `:322`) both target the
    primary, so they should remain green **unchanged**.
  - `tests/test_config.py` must not be touched at all.

If your work requires changing anything beyond the above, say so explicitly in the handoff with the
reason.

## Definition of done

- `src/gateway/adapters/sharellm.py`, `src/gateway/adapters/sanitizer.py` (or `base.py`) and
  `tests/test_adapters.py` exist with complete implementations.
- `pytest` passes from the project root: the 3 config tests, the 9 proxy tests, the 13 failover
  tests and the new adapter tests.
- No real network ports are bound.
