# Builder Handoff — Task 1.4

**STATUS: COMPLETE**

Task: *Task 1.4 — Target Model Mapping, ShareLLM Adapter & Telemetry* for the
ShareLLM Smart Gateway (`C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway`).
Four deliverables: (1) dynamic per-target model rewriting, (2) ShareLLM error
normalization, (3) prompt-cache telemetry, (4) an anti-422 parameter sanitizer,
all wired into the request/response lifecycle, fully typed, with the 25 prior
tests still green.

---

## 1. Files created / changed

| Path (absolute) | Purpose |
|---|---|
| `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway/src/gateway/adapters/sharellm.py` | **NEW.** ShareLLM adapter: `sanitize_error_envelope`, pure `extract_cache_stats`, `format_cache_log` / `log_cache_stats`. |
| `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway/src/gateway/adapters/sanitizer.py` | **NEW.** Anti-422 request-parameter sanitizer (`sanitize_request_payload`). |
| `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway/tests/test_adapters.py` | **NEW.** 35 tests covering the four requirements + all decided edge cases. |
| `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway/src/gateway/engine/router.py` | **MODIFIED.** Per-target `model` rewrite on a payload copy; `dispatch` now also reports the serving provider name. |
| `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway/src/gateway/main.py` | **MODIFIED.** Sanitize → dispatch → normalize-error → cache-log integration. |
| `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway/src/gateway/adapters/__init__.py` | **MODIFIED (doc-only).** The package docstring said "intentionally empty for now"; it is no longer empty. 3-line docstring correction, no code. |
| `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway/tests/test_failover.py` | **MODIFIED.** Lines ~168–180 only, per the blast-radius section. |

Untouched, as required: `tests/test_config.py`, `tests/test_proxy.py`,
`config.example.yaml`, everything under `analysis/` (except this handoff file).

---

## 2. Exact diff of every existing test file touched

### `tests/test_failover.py` — the **only** existing test file modified

Only `test_buffered_failover_on_retryable_status` changed. `test_proxy.py` and
`test_config.py` were **not** touched (verified: `test_config.py` is 4859 bytes,
SHA-256 `fd203d6c652d9c523c2f6840e7e45bd9e08cf347aad2e548ed5152b77c1a2f90` — the
required `fd203d6c652d9c52…`).

**BEFORE** (original lines 168–180):

```python
        assert primary.call_count == 1
        assert backup.call_count == 1
        # The payload is forwarded verbatim (Task 1.2's transparency contract):
        # the gateway relays the caller's JSON body unchanged rather than
        # rewriting `model` to the target's upstream identifier.
        assert (
            json.loads(primary.calls.last.request.content)
            == _inbound_payload(stream=False)
        )
        assert (
            json.loads(backup.calls.last.request.content)
            == _inbound_payload(stream=False)
        )
```

**AFTER** (current lines 168–181):

```python
        assert primary.call_count == 1
        assert backup.call_count == 1
        # Task 1.4 rewrites `model` to each target's upstream identifier. The
        # inbound alias is "gpt-6.1-sol" — the primary target's own model — so
        # the primary body is forwarded unchanged; the backup target declares
        # "gpt-6-astra", so its outbound body carries that model id rather than
        # the inbound alias.
        assert (
            json.loads(primary.calls.last.request.content)
            == _inbound_payload(stream=False)
        )
        assert (
            json.loads(backup.calls.last.request.content)
            == {**_inbound_payload(stream=False), "model": BACKUP_TARGET.model}
        )
```

**Justification.** The stale comment claimed verbatim forwarding (Task 1.2's
contract); Task 1.4 supersedes it with per-target rewriting. The **primary**
assertion is unchanged because `PRIMARY_TARGET.model == "gpt-6.1-sol" ==
MODEL`, so the rewrite is a no-op there. The **backup** assertion now expects
`{**_inbound_payload(stream=False), "model": BACKUP_TARGET.model}`, i.e. exactly
`{"model": "gpt-6-astra", "messages": [{"role": "user", "content": "Hi"}],
"stream": false}` — the spec-mandated backup body. `BACKUP_TARGET.model` is the
config-derived constant equal to `"gpt-6-astra"`. Nothing else in the file
changed; no assertion was weakened (the backup check is strictly stronger than
before, since it now pins the rewritten model id).

---

## 3. Key design decisions (all seven points)

### #1 — Where each concern lives, and the ordering

* **Model rewrite → inside `dispatch()`'s loop** (`router.py`). It is
  per-target, so it must sit next to the target selection:
  `target_payload = {**request_data, "model": target.model}`. A **shallow copy**
  is built per attempt — the caller's dict is never mutated. Nested values
  (`messages`, `tools`, …) are shared read-only, which is safe because nothing
  mutates them.
* **Parameter sanitization → `main.py`, before `dispatch`.** It is per-request
  and provider-agnostic.
* **Ordering — sanitize first, then rewrite `model` per target.** Correct
  because the two operate on orthogonal keys: the sanitizer touches only
  `tools` / `tool_choice` / `stream_options`, while the rewrite touches only
  `model`. Sanitizing first means the engine receives a clean body and then
  builds one rewritten copy per target; doing it the other way round would run
  the anti-422 guard against an engine-constructed body (and would have to
  re-run per attempt). The caller's alias is resolved to a route *before*
  sanitization (the sanitizer never touches `model`), so routing is unaffected.

### #2 — The cache-logging signature

The spec pins `extract_cache_stats(response_json: dict) -> dict | None` yet
describes a log line ending `via <provider>`, which `response_json` cannot
supply. Resolution:

* `extract_cache_stats` is **pure** — returns the stats dict or `None`, no
  logging.
* The `[CACHE HIT] N/M tokens (P%) via <provider>` line is emitted at the
  integration point in `main.py`, where the provider is known, via the small
  helper `log_cache_stats(stats, provider)` in `sharellm.py` (with a pure
  `format_cache_log` for deterministic testing).
* **How the provider becomes known:** `dispatch` now returns
  `(result, provider_name)` (`BufferedDispatch` / `StreamingDispatch` aliases),
  where `provider_name` is the name of the provider that actually served the
  request (`None` on exhaustion). This is the cleanest way to attribute a
  response to the upstream that produced it, which after a failover is **not**
  necessarily the route's first target. See §6 "Changes beyond the stated blast
  radius" for the justification.

### #3 — What `extract_cache_stats` returns

Concrete shape (a `TypedDict`, so the returned value and the logged string
cannot drift):

```python
{"cached_tokens": int, "prompt_tokens": int, "cache_hit_pct": float}
```

`cache_hit_pct = round(cached_tokens / prompt_tokens * 100.0, 1)` — one decimal
place, matching the spec's `87.5%`. The log line formats the **already-rounded**
value (`f"{pct:.1f}"`), so the returned value and the emitted string are always
consistent. Example verified: `3840 / 4387 → 87.5%`.

### #4 — Guarding malformed input

`extract_cache_stats` returns `None` (never raises) for: missing / non-mapping
`usage`; missing / non-numeric / non-positive `prompt_tokens` (covers division
by zero); missing / non-mapping `prompt_tokens_details`; missing / non-numeric /
negative `cached_tokens`. `bool` is rejected explicitly (it is an `int`
subclass but not a token count).

`sanitize_error_envelope` handles: a missing or non-mapping `error` member; a
missing `type`; a missing `message`; and a `code` that is absent or `None` —
`message` and `code` are preserved when present and **never invented**. The
input mapping is not mutated.

### #5 — `sanitize_error_envelope` status precedence and the untouched rule

Rule, highest precedence first, applied **only** when
`error.type == "share_llm_error"`:

1. `status == 429` **or** the message contains `"limit"` / `"overloaded"` →
   `rate_limit_error`;
2. `status == 400` → `invalid_request_error`;
3. any other `5xx` → `api_error`;
4. **otherwise** (e.g. 401 / 403 / 404 / 422) → **the upstream `type` is left
   unchanged**.

A **non-`share_llm_error`** envelope (including the gateway's own
`gateway_failover_exhausted`) is returned **completely untouched**. Rationale:
the spec only maps 429/400/5xx, so inventing a mapping for 401/403/404/422 would
be guessing; the conservative reading is to change nothing the spec did not ask
to change. Rate-limit wins over the status because the spec lists it first and
because a `"limit"`/`"overloaded"` message is a genuine rate-limit signal even
on a 5xx.

### #6 — Parameter sanitizer rules (precise and conservative)

`sanitize_request_payload` removes **exactly**:

* `tools` when it is `None` or `[]`;
* `tool_choice` when it is `None` or the string `"none"`;
* `stream_options` when the request is **not streaming** (`stream` absent or
  falsy) **or** when its value is **not a JSON object**.

Nothing else is stripped — a non-empty `tools`, `tool_choice` of
`"auto"`/`"required"`/a function object, and every unrelated field survive. A
payload with **no** targeted field is returned **byte-identical** (in fact the
same object: `json.dumps(sanitized) == json.dumps(payload)` and
`sanitized is payload`). This is what keeps Task 1.2's transparency tests green.
The input mapping is never mutated.

Note on `None`: the spec says "unsupported **null/empty** tool fields", so
`tools: None` / `tool_choice: None` are treated as the "null" half of that
phrase and removed too. Removing them is intent-preserving (both mean "no
tools", which is the default).

### #7 — Where errors get normalized

`sanitize_error_envelope` is applied in `main.py` by the helper
`_normalize_error_body(status_code, body)`, which acts only when
`status_code >= 400` and the body is a JSON object. It therefore covers:

* **(a)** a non-retryable upstream error body relayed straight to the client
  (e.g. a 400 `share_llm_error` → `invalid_request_error`), on **both** the
  buffered and the streaming-error path;
* **(b)** the **failover-exhausted** path. The gateway's own envelope has
  `type == "gateway_failover_exhausted"` (non-`share_llm_error`), so by the rule
  in #5 it is **returned untouched** — the existing failover tests that pin that
  exact envelope stay green. This is deliberate: the gateway must not rewrite
  its own envelope, and there is no cache telemetry on an error.

---

## 4. Verification evidence (real output)

All commands run from the project root with
`C:/Users/Notebook/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe`.

### 4.1 Full suite

```
$ python -m pytest -v
... (60 lines, all PASSED) ...
============================= 60 passed in 4.18s ==============================
```

Per-file collection counts (3 + 9 + 13 + 35 = 60):

```
tests/test_config.py    -> 3 tests collected
tests/test_proxy.py     -> 9 tests collected
tests/test_failover.py  -> 13 tests collected
tests/test_adapters.py  -> 35 tests collected
```

Baseline before my changes was `25 passed in 3.26s`; now `60 passed`. The 25
prior tests all still pass.

### 4.2 Rewrite reaches the backup + no mutation of the caller's dict

Real outbound bodies captured from respx in one failover (inbound alias
`gpt-6-astra`):

```
inbound alias payload : {"model": "gpt-6-astra", "messages": [{"role": "user", "content": "Hi"}], "stream": false}
PRIMARY outbound body : {"model":"gpt-6.1-sol","messages":[{"role":"user","content":"Hi"}],"stream":false}
BACKUP  outbound body : {"model":"gpt-6-astra","messages":[{"role":"user","content":"Hi"}],"stream":false}
serving provider      : openrouter_backup
caller dict unchanged : True
```

The primary receives its own model (`gpt-6.1-sol`, unchanged); the backup
receives `gpt-6-astra`; the caller's dict is untouched (the rewrite is on a
copy).

### 4.3 Cache log format (real emitted line)

```
response status       : 200
emitted log line      : '[CACHE HIT] 3840/4387 tokens (87.5%) via sharellm_primary'
matches spec format   : True
```

Matches `[CACHE HIT] N/M tokens (P%) via <provider>` exactly.

### 4.4 No-tools payload unchanged

```
json.dumps(payload) == json.dumps(sanitized): True
same object returned  : True
```

### 4.5 Zero leaks still hold

```
$ python -m pytest tests/test_proxy.py::test_client_disconnect_releases_upstream_stream_at_disconnect_time \
                   tests/test_failover.py::test_client_disconnect_during_failover_releases_every_stream -v
tests/test_proxy.py::test_client_disconnect_releases_upstream_stream_at_disconnect_time PASSED [ 50%]
tests/test_failover.py::test_client_disconnect_during_failover_releases_every_stream PASSED [100%]
============================== 2 passed in 0.69s ==============================
```

The streaming path was not restructured; the disconnect-time release assertions
(including the failover variant) pass unchanged.

### 4.6 No real ports bound

A `sys.addaudithook` probe around the whole suite recorded **zero
`socket.listen` events**; the only `socket.bind` events are all
`('127.0.0.1', 0)` — ephemeral, i.e. the Windows `ProactorEventLoop`
self-pipe. A bare `asyncio.run(lambda: None)` with no gateway code produces
exactly the same single `('socket.bind', ('127.0.0.1', 0))`, confirming the
attribution:

```
$ (audit hook) python -m pytest -q tests
60 passed in 3.48s
event counts: {'socket.bind': 24, 'socket.connect': 24}
distinct bind addresses  : [('127.0.0.1', 0)]
any socket.listen events : []

$ (audit hook) bare asyncio.run()
bare asyncio.run() bind/listen events: [('socket.bind', ('127.0.0.1', 0))]
```

Also, a direct connect to the configured gateway port (`127.0.0.1:8000`) is
refused (`TimeoutError`), so nothing is listening:

```
PORT 8000 : not listening -> TimeoutError timed out
```

### 4.7 `tests/test_config.py` byte-identical

```
tests/test_config.py: 4859 bytes fd203d6c652d9c523c2f6840e7e45bd9e08cf347aad2e548ed5152b77c1a2f90
config.example.yaml  : 1442 bytes 65e12e3884a7bf25278f595278b6f15d9deb9bd244dfb1bb797a9909d46f81d7
```

SHA-256 prefix `fd203d6c652d9c52…`, 4859 bytes — matches the required value;
`config.example.yaml` was not modified.

---

## 5. Evidence package (minimal reviewer reading list)

1. `src/gateway/adapters/sharellm.py` — error normalization + pure cache extractor.
2. `src/gateway/adapters/sanitizer.py` — anti-422 parameter sanitizer.
3. `src/gateway/engine/router.py` — lines ~38–75 (aliases) and ~253–306 (rewrite + provider reporting).
4. `src/gateway/main.py` — lines ~33–45 (imports), ~93–110 (`_normalize_error_body`), ~235–285 (endpoint integration).
5. `tests/test_adapters.py` — the 35 new tests.
6. `tests/test_failover.py` — lines 168–181 (the only existing-test change).

---

## 6. Known limitations / follow-ups (and changes beyond the stated blast radius)

**Changes beyond the declared blast radius (explicitly justified):**

1. **`dispatch` now returns `(result, provider_name)` instead of the bare
   result** (`router.py`). The task's design decision #2 requires the
   `[CACHE HIT] … via <provider>` line to be emitted "where the provider is
   known", and the only place that knows which provider actually served a
   request is inside `dispatch`'s loop (failover makes it non-deterministic
   from `main.py`). Reporting it as a return value is the cleanest, side-effect
   free way to do that. **No *pre-existing* test imports or calls `dispatch`**
   — the only caller in the test suite is the new
   `tests/test_adapters.py::test_dispatch_rewrites_model_per_target_and_never_mutates_the_caller`
   added by this task — so the signature change required **zero** edits to the
   existing tests beyond the mandated `test_failover.py` lines;
   the `BufferedResult` / `StreamingResult` aliases in `proxy.py` are
   unchanged, and `forward_request`'s contract is untouched. The
   `gateway_failover_exhausted` return carries `provider_name = None`.
2. **`adapters/__init__.py` docstring** updated from "intentionally empty for
   now" to describe the two modules now present. Doc-only, no behaviour.

**Ambiguities the spec left open (decisions documented in §3):**

* ShareLLM errors on statuses the spec does not map (401/403/404/422) keep
  their upstream `type`; no mapping is invented.
* `tools: None` / `tool_choice: None` are removed as the "null" half of the
  spec's "null/empty tool fields".
* `stream_options` is kept only when the request streams **and** the value is a
  JSON object.

**Not done / not verifiable here:**

* **`mypy` was not run *in round 1*** — it was not installed in the provided
  interpreter. *(Superseded: `mypy` 2.4.0 + `types-PyYAML` were installed by the
  orchestrator after review, the one `--strict` error was fixed and a
  `[tool.mypy]` config added — see `## FIXES APPLIED (round 1)` → F5. mypy is now
  clean under both `src/gateway` and `--strict`.)*
* The suite exercises the code through `httpx.ASGITransport` + `respx`; no live
  ShareLLM upstream was contacted (correct for a deterministic, port-free
  suite).

---

## FIXES APPLIED (round 1)

Review: `analysis/agent_reviews/review-1.4-1.md` (verdict CHANGES_REQUIRED, one
MAJOR + two MINOR + two NIT). All findings addressed; **F2 was intentionally
left unchanged** (see below). Suite is now **61 passed**.

### F1 — MAJOR — over-sanitization of `tool_choice` → **FIXED**

*File:* `src/gateway/adapters/sanitizer.py`.

`tool_choice` is now stripped **only when it is a no-op** — i.e. only when the
payload declares no non-empty `tools` list. Added the helper
`_has_declared_tools(payload)` (`isinstance(tools, list) and len(tools) > 0`);
the `tool_choice` branch now reads:

```python
    if "tool_choice" in sanitized and not _has_declared_tools(sanitized):
        tool_choice = sanitized["tool_choice"]
        if tool_choice is None or tool_choice == "none":
            del sanitized["tool_choice"]
            changed = True
```

The `tools` handling still runs first, so `tools: None` / `[]` are deleted
before this check (making the choice genuinely a no-op). The module and function
docstrings were rewritten to state the rule explicitly ("`tool_choice` is
removed only when there are no tools to choose from"). Everything else is
untouched: `tools: None/[]` removed; non-empty `tools` kept; `tool_choice`
`"auto"`/`"required"`/object kept; `stream_options` rules unchanged; no-tool
payload returned as the same object.

**Before / after for the F1 case** (`BEFORE` = the pre-fix algorithm run
verbatim; `AFTER` = the real function):

```
in       : {"model": "m", "messages": [], "tools": [{"type": "function", "function": {"name": "f", "parameters": {}}}], "tool_choice": "none"}
BEFORE   : {"model": "m", "messages": [], "tools": [{"type": "function", "function": {"name": "f", "parameters": {}}}]}
AFTER    : {"model": "m", "messages": [], "tools": [{"type": "function", "function": {"name": "f", "parameters": {}}}], "tool_choice": "none"}
tool_choice preserved after fix: True
```

**Neighbouring cases unchanged** (real output):

```
tools:[] + tool_choice:"none" (both stripped)     out={"model": "m", "messages": []}                     same_obj=False
tool_choice:"none", no tools (stripped)           out={"model": "m", "messages": []}                     same_obj=False
non-empty tools + tool_choice:"auto" (both kept)  out={..., "tools": [...], "tool_choice": "auto"}       same_obj=True
non-empty tools + tool_choice:"none" (both kept)  out={..., "tools": [...], "tool_choice": "none"}       same_obj=True
non-empty tools + tool_choice:None (both kept)    out={..., "tools": [...], "tool_choice": null}         same_obj=True
no-tool payload (byte-identical)                  out=in                                                 same_obj=True
tools:None + tool_choice:None (both stripped)     out={"model": "m", "messages": []}                     same_obj=False
```

### F3 — MINOR — missing test → **FIXED**

*File:* `tests/test_adapters.py` — added
`test_non_empty_tools_preserve_tool_choice` (right after
`test_non_empty_tools_and_real_tool_choice_survive`). It pins both the
`"none"` and `None` cases with a **non-empty `tools`** list, asserting
`tools` and `tool_choice` both survive. **Discriminating** — verified that the
pre-fix algorithm drops `tool_choice`, so `sanitized["tool_choice"]` raises
`KeyError` and the test fails against the old behaviour:

```
pre-fix DROPPED tool_choice -> the new test would FAIL pre-fix (discriminating)
```

The existing `tools: []` + `"none"` test (both stripped) and the non-empty
`tools` + `"auto"` test (both kept) are unchanged.

### F4 — NIT — handoff wording → **FIXED**

*File:* `analysis/builder_handoff_1_4.md` (the `dispatch` justification in
§6). Reworded to: **"No *pre-existing* test imports or calls `dispatch`"**, and
named the new `test_adapters.py::test_dispatch_rewrites_model_per_target_and_never_mutates_the_caller`
as the only test-side caller. The material claim (zero edits to existing tests)
is unchanged.

### F5 — MINOR — make the typing constraint verifiable → **FIXED**

1. *File:* `src/gateway/engine/proxy.py` — `_read_error_body` now binds the
   decoded body to a `JSONValue`-annotated local before returning it (the same
   narrowing `_forward_buffered` already uses), so the declared return type
   holds without leaking `httpx.Response.json()`'s `Any`. The return type was
   **not** widened and behaviour is identical (parsed JSON, or the wrapped
   non-JSON error, always `aclose()`d in `finally`).
2. *File:* `pyproject.toml` — added, exactly as specified:

   ```toml
   [tool.mypy]
   strict = true
   files = ["src/gateway"]
   ```

**mypy output (all three, real):**

```
$ python -m mypy src/gateway
Success: no issues found in 10 source files

$ python -m mypy --strict src/gateway
Success: no issues found in 10 source files

$ python -m mypy            # picks up [tool.mypy]
Success: no issues found in 10 source files
```

`tests/` was not run through mypy (out of scope, not configured).

### F6 — NIT — assert identity → **FIXED**

*File:* `tests/test_adapters.py`,
`test_payload_without_tool_fields_is_returned_unchanged` now also asserts
`sanitized is payload` **and** `sanitize_request_payload(payload) is payload`,
locking the "same object" guarantee the handoff claims.

### F2 — intentionally NOT changed

The unanchored `"limit"` / `"overloaded"` substring matching in
`src/gateway/adapters/sharellm.py` (`_RATE_LIMIT_MARKERS`, used by
`_normalized_error_type`) is **left exactly as it is** — no word-boundary
matching added. The spec says the message "contains `\"limit\"`", so substring
matching is spec-literal, and the Reviewer classified it as spec-compliant and
escalated it to the user as a separate judgment call.

### Round-1 verification

```
$ python -m pytest -q
61 passed in 3.95s
```

Per-file: 3 config + 9 proxy + 13 failover + 36 adapters = 61.
`tests/test_config.py` still byte-identical: 4859 bytes, SHA-256
`fd203d6c652d9c523c2f6840e7e45bd9e08cf347aad2e548ed5152b77c1a2f90`.
