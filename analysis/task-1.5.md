# Task 1.5 — Capstone: E2E Chaos Test, CLI Banner & Milestone 1 Freeze

Project: **ShareLLM Smart Gateway** (`sharellm-gateway`)
Project root: `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway`

Task 1.4 is complete and verified — 61 tests passing, `mypy --strict` clean across 10 source files.
This task finalizes Sprint 1 (Milestone 1 MVP).

## Objective

1. Resolve the F2 judgment call with word-boundary matching.
2. Implement an informative CLI startup banner and entrypoint.
3. Write an end-to-end chaos test simulating the full developer journey.
4. Freeze and tag Milestone 1.

## Scope of work

### 1. Refine rate-limit matching (F2 fix)

In `src/gateway/adapters/sharellm.py`:

- Update `sanitize_error_envelope` to match rate-limit keywords using word boundaries
  `r"\b(limit|limits|overloaded)\b"` (case-insensitive).
- Ensure a string like `"unlimited tokens available"` on a **400** status does **not** falsely map to
  `rate_limit_error`.

### 2. CLI polish & informative startup banner

In `src/gateway/main.py`:

- Add `__main__.py` in `src/gateway/` so the gateway can be run via `python -m gateway`.
- When starting via CLI (`run_cli`):
  - Parse an optional `--config` flag (default: `config.yaml`, or fall back to
    `config.example.yaml` if `config.yaml` does not exist).
  - Print a clean ASCII startup banner showing:
    - Gateway URL: `http://<host>:<port>/v1`
    - Active configured routes and their fallback targets.
    - Quick setup tip for Cursor/Cline: `Set Base URL to http://127.0.0.1:8000/v1`.

### 3. End-to-end chaos integration test (`tests/test_e2e_chaos.py`)

Simulate a realistic developer coding session in Cursor:

- **Inbound client request:** streaming `POST /v1/chat/completions` for model alias `gpt-6.1-sol`,
  carrying a tools schema.
- **Upstream target 1 (`sharellm_primary`):** immediately fails with HTTP 429 and
  `{"error": {"message": "channel rate limit exceeded", "type": "share_llm_error"}}`.
- **Gateway interception:** catches the 429, prevents a client disconnect, rewrites the model to the
  backup target (`gpt-6-astra`), and fails over.
- **Upstream target 2 (`openrouter_backup`):** responds with HTTP 200 and a multi-chunk SSE stream
  containing prompt-cache details.

Verify:

1. The client receives HTTP 200 and an unbroken SSE stream.
2. The sanitizer cleaned unsupported tool fields before they reached target 2.
3. The cache telemetry log is emitted.
4. Cooldown is applied to target 1 for subsequent requests.

### 4. Milestone 1 verification

- Run the complete test suite (`pytest -v`).
- Run `mypy --strict` across all source files.
- Verify zero socket leaks on client disconnects.

## Resolution of the cache-telemetry ambiguity (explicit instruction)

Task 1.4 scoped cache telemetry to **non-streaming** responses, and the gateway deliberately does not
parse SSE bodies (zero-copy streaming). The chaos test's verification 3 asks for a cache telemetry log
inside a **streaming** journey. These are reconciled as follows — implement it this way:

The chaos journey has **two legs**, which also makes verification 4 coherent (it explicitly says
"for subsequent requests"):

- **Leg A (streaming):** the full failover journey above — 429 on target 1, failover, 200 SSE relayed
  unbroken. Assertions 1 and 2.
- **Leg B (follow-up non-streaming request):** a second request in the same session that (a) is routed
  **directly to target 2** because target 1 is cooling down, and (b) returns a body carrying
  `usage.prompt_tokens_details.cached_tokens`, so the `[CACHE HIT] …` line is emitted. Assertions 3
  and 4.

Do **not** add SSE body parsing. Document this interpretation explicitly in the handoff, including the
fact that a streaming response's own cache details are not logged.

## Constraints & coding standards

- **Backward compatibility:** all 61 existing tests must remain green. `tests/test_config.py` must not
  be modified.
- Zero leaks; fully typed — `mypy --strict` must stay clean with the existing `[tool.mypy]` config
  (`strict = true`, `files = ["src/gateway"]`).
- Deterministic, zero-network-bound tests. **Never call `run_cli()` from a test** — it binds a port and
  blocks.
- ASCII only in the banner (no box-drawing or emoji characters).

## Testability requirement for the CLI (important)

Do not put the banner/arg logic inline in a way that cannot be tested. Factor it into small pure
functions that tests can exercise without starting a server, e.g.:

- `parse_args(argv: Sequence[str]) -> argparse.Namespace`
- `resolve_config_path(explicit: str | Path | None) -> Path` — implements the
  `config.yaml` → `config.example.yaml` fallback and raises a clear error if neither exists.
- `format_startup_banner(config: AppConfig) -> str` — pure, returns the banner text.
- `run_cli(argv: Sequence[str] | None = None) -> None` — ties them together and calls `uvicorn.run`.

Tests should assert on the pure functions and, if they cover `run_cli` at all, must monkeypatch
`uvicorn.run` so nothing binds.

## Definition of done

- `src/gateway/__main__.py` exists and `python -m gateway --help` works without binding a port.
- `tests/test_e2e_chaos.py` exists and passes.
- `pytest -v` green across all suites; `mypy --strict` clean; no real network ports bound.
