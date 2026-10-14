# Builder Handoff — Task 1.1

STATUS: CHANGES_APPLIED (round 1 — see `## FIXES APPLIED (round 1)` at the bottom)

## TASK

Task 1.1 — Repository Scaffold & Typed Config Engine for **ShareLLM Smart Gateway**.
Create the repository layout under `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway`
and implement a strictly typed, Pydantic v2 + PyYAML configuration engine:
models `ProviderConfig`, `RouteTargetConfig`, `RouteConfig`, `GatewayServerConfig`, `AppConfig`
and `load_config(path: str | Path = "config.yaml") -> AppConfig` that fails fast with a
descriptive `ValueError` when a route target references a provider that is not declared.
Plus `pyproject.toml` (src layout, pytest `pythonpath`), `.gitignore`, `config.example.yaml`,
package `__init__.py` files and two tests.

## FILES CREATED (all absolute paths)

- `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway/.gitignore` — standard Python ignore set (bytecode, venvs, `.env`, `config.yaml`, `*.json`, build/dist, pytest/mypy/ruff caches, egg-info, IDE/OS cruft).
- `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway/pyproject.toml` — setuptools build, project metadata, core + `dev` dependencies, src-layout discovery, and `[tool.pytest.ini_options]` (`testpaths`, `pythonpath = ["src"]`).
- `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway/config.example.yaml` — reference config: 2 providers, 2 failover routes.
- `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway/src/gateway/__init__.py` — package surface; re-exports the public config API via `__all__`; defines `__version__`.
- `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway/src/gateway/config.py` — the typed config engine (models + `load_config`).
- `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway/src/gateway/engine/__init__.py` — scaffolding package with docstring (routing/failover/retry engine to live here).
- `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway/src/gateway/adapters/__init__.py` — scaffolding package with docstring (sharellm/openai adapters to live here).
- `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway/src/gateway/utils/__init__.py` — scaffolding package with docstring (logging/timing/token helpers to live here).
- `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway/tests/__init__.py` — empty test package marker.
- `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway/tests/test_config.py` — the two required tests.

Nothing outside the project root was touched; the pre-existing `analysis/` folder is untouched.

## KEY DESIGN DECISIONS

1. **Where the provider-reference check lives.** The invariant is enforced in **two
   complementary layers**:
   - `AppConfig._validate_provider_references` — a `@model_validator(mode="after")` that calls
     `_assert_provider_references(...)`. This guarantees an `AppConfig` (and thus any route with a
     dangling `target.provider`) **cannot be constructed at all**, including via a direct
     `AppConfig(...)` / `AppConfig.model_validate(...)` call.
   - `load_config` calls `_assert_raw_provider_references(raw)` on the parsed mapping **before**
     model construction. This is required because Pydantic wraps any exception raised inside a
     validator into `pydantic.ValidationError` (verified empirically — see EVIDENCE), and the task
     demands the loader surface a **plain** `ValueError` that is "not … converted into something
     else". Doing the check on the raw mapping first means `load_config` raises
     `type(exc) is ValueError` (verified `True`).
   Both layers share the single `_unknown_provider_message(...)` helper, so the message text has one
   source of truth. The message names the offending route, the offending provider value and the
   sorted list of known provider keys.

2. **Defaults live in the model.** `GatewayServerConfig.host/port/log_level`,
   `RouteConfig.strategy` and `RouteTargetConfig.timeout_seconds` all carry `Field(default=...)`,
   so a YAML file that omits `timeout_seconds`, `strategy` or the whole `gateway:` section still
   validates. `AppConfig.gateway` uses `default_factory=GatewayServerConfig`; `providers`/`routes`
   use `default_factory=dict`.

3. **Pydantic v2 idioms only.** `Field(default=..., description=...)`, `ConfigDict(extra="forbid")`
   on every model, `model_validator(mode="after")`. No deprecated v1 `@validator`.
   `ProviderConfig.type` is `Literal["sharellm", "openai"]`. `base_url` is a plain `str` (not
   `HttpUrl`) so values compare equal to the YAML strings.

4. **Error handling in `load_config`.** Missing file → `FileNotFoundError` with an explicit
   resolved path (not a bare traceback). YAML that parses to `None` → `ValueError` explaining the
   file is empty. Non-mapping top level → `ValueError` naming the parsed type. Only then is the
   reference check run and `AppConfig.model_validate(raw)` applied.

5. **Test resolves `config.example.yaml` relative to the test file**:
   `Path(__file__).resolve().parents[1] / "config.example.yaml"`, so the suite passes regardless of
   the runner's working directory (verified from an unrelated CWD).

6. **`config.example.yaml`** declares `sharellm_primary` (`type: sharellm`) and
   `openrouter_backup` (`type: openai`) with realistic `base_url`s and placeholder keys, plus two
   routes: `coding_route` (`models: ["gpt-6.1-sol", "gpt-6-astra"]`, `strategy: failover`, ordered
   targets primary→backup) and `chat_route`, exercising the multi-route shape.

## VERIFICATION EVIDENCE (verbatim)

### Command 1 — full suite from the project root

```
cd "C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway" && C:/Users/Notebook/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe -m pytest -v
```

Output:

```
============================= test session starts =============================
platform win32 -- Python 3.13.14, pytest-9.1.1, pluggy-1.6.0 -- C:\Users\Notebook\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe
cachedir: .pytest_cache
rootdir: C:\Users\Notebook\WorkBuddy AI\2026-10-14-19-08-04\sharellm-gateway
configfile: pyproject.toml
testpaths: tests
plugins: anyio-4.15.1, asyncio-1.4.0, respx-0.23.1
asyncio: mode=Mode.STRICT, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 2 items

tests/test_config.py::test_example_config_loads_and_resolves_references PASSED [ 50%]
tests/test_config.py::test_unknown_provider_reference_raises PASSED      [100%]

============================== 2 passed in 0.19s ==============================
```

Summary line: `2 passed in 0.19s`

### Command 2 — end-to-end load outside pytest (`sys.path` includes `src`)

`load_config("config.example.yaml")` returned a valid `AppConfig`; key extracts:

```
gateway.port = 8000
providers    = ['openrouter_backup', 'sharellm_primary']
routes       = ['chat_route', 'coding_route']
coding order = ['sharellm_primary', 'openrouter_backup']
```

(Full `model_dump_json(indent=2)` output also confirmed `coding_route.models == ["gpt-6.1-sol", "gpt-6-astra"]`, `strategy == "failover"`, `targets[0].timeout_seconds == 15.0`.)

### Command 3 — deliberately broken reference raises a PLAIN ValueError

YAML with a route target `provider: ghost_provider` (only `sharellm_primary` declared):

```
Traceback (most recent call last):
  File "<string>", line 30, in <module>
    load_config(p)
  File "...\src\gateway\config.py", line 244, in load_config
    _assert_raw_provider_references(raw)
  File "...\src\gateway\config.py", line 201, in _assert_raw_provider_references
    raise ValueError(
        _unknown_provider_message(str(route_name), str(provider), known_providers)
    )
ValueError: Route 'coding_route' target references unknown provider 'ghost_provider'. Known providers: ['sharellm_primary'].
```

Exception introspection:

```
EXC TYPE : ValueError
EXC MODULE: builtins
type(exc) is ValueError : True
isinstance ValueError   : True
EXC MSG  : Route 'coding_route' target references unknown provider 'ghost_provider'. Known providers: ['sharellm_primary'].
```

### Command 4 — model-level guard, file-error paths and defaults

```
=== direct AppConfig.model_validate with dangling ref (model guard) ===
type: ValidationError | is ValueError: True
msg : Value error, Route 'coding_route' target references unknown provider 'ghost_provider'. Known providers: ['sharellm_primary']. [...]

=== direct AppConfig(...) constructor with dangling ref ===
type: ValidationError | is ValueError: True
msg : Value error, Route 'r' target references unknown provider 'ghost'. Known providers: ['p']. [...]

=== missing file ===
type: FileNotFoundError
msg : Configuration file C:\Users\Notebook\AppData\Local\Temp\tmp948yr6ag\nope.yaml (expected an existing YAML file).

=== empty file (parses to None) ===
type: ValueError
msg : Configuration file C:\Users\Notebook\AppData\Local\Temp\tmp948yr6ag\empty.yaml is empty or contains only comments; expected a YAML mapping with 'gateway', 'providers' and 'routes' sections.

=== non-mapping top level ===
type: ValueError
msg : Configuration file C:\Users\Notebook\AppData\Local\Temp\tmp948yr6ag\list.yaml must contain a top-level YAML mapping, but parsed to list.

=== defaults: minimal config omitting timeout_seconds/strategy/gateway ===
gateway defaults: {'host': '127.0.0.1', 'port': 8000, 'log_level': 'info'}
strategy default: failover
timeout default : 15.0
```

### Command 5 — CWD robustness (run from an unrelated directory)

```
cd "C:/Users/Notebook/AppData/Local/Temp" && <python> -m pytest "C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway/tests/test_config.py" -v
...
============================== 2 passed in 1.19s ==============================
```

### Command 6 — public API surface

```
gateway.__all__ = ['AppConfig', 'GatewayServerConfig', 'ProviderConfig', 'RouteConfig', 'RouteTargetConfig', 'load_config']
gateway.__version__ = 0.1.0
public re-exports OK: load_config AppConfig
subpackages import OK
```

Final tree (excluding `__pycache__`, `.pytest_cache`, `analysis`):

```
./.gitignore
./config.example.yaml
./pyproject.toml
./src/gateway/__init__.py
./src/gateway/config.py
./src/gateway/adapters/__init__.py
./src/gateway/engine/__init__.py
./src/gateway/utils/__init__.py
./tests/__init__.py
./tests/test_config.py
```

## ASSUMPTIONS

- "The public entry point" is `load_config`; the spec's requirement that a dangling reference can
  "never be constructed through the public entry point" is read as applying to `load_config`. The
  model validator additionally covers direct construction, so both readings are satisfied.
- `ProviderConfig.base_url` is modelled as `str` (not `HttpUrl`) so YAML strings compare equal.
- `RouteConfig.strategy` is a free-form `str` with default `"failover"` (spec calls it "a string"),
  not a `Literal`, to avoid over-constraining future strategies.
- `extra="forbid"` was applied to all models to catch YAML typos (a `ConfigDict` use). This is a
  deliberate strictness choice; no example/test config carries unknown keys.
- `min_length=1` was added to `RouteConfig.models` and `RouteConfig.targets` (a route with no
  models or no targets is meaningless).
- The second route (`chat_route`) reuses the same failover shape; the spec left its contents open.

## KNOWN LIMITATIONS / FOLLOW-UPS

- The reference-check rule is expressed twice (raw-mapping pass in `load_config` + model validator),
  sharing one message helper. This is intentional: it is the only way to have both a plain
  `ValueError` from `load_config` **and** an un-constructible `AppConfig`, because Pydantic always
  wraps validator exceptions into `ValidationError`.
- The raw-mapping pass is defensive: if `routes`/`targets` are structurally malformed it skips and
  lets `model_validate` produce the precise structural error, so the two passes never conflict.
- No `mypy`/`ruff` run was performed (not installed in the provided environment and not required by
  the task); typing is enforced by full annotations + Pydantic.
- The `engine/`, `adapters/` and `utils/` packages are intentionally empty scaffolding per the task.

## EVIDENCE PACKAGE (minimal reviewer reading list)

1. `src/gateway/config.py`
2. `tests/test_config.py`
3. `config.example.yaml`
4. `pyproject.toml`
5. `src/gateway/__init__.py`

## Handoff comment

Implemented the full Task 1.1 scaffold. The config engine is in
`src/gateway/config.py` (five Pydantic v2 models + `load_config`); the reference check is enforced
by an `AppConfig` `model_validator` *and* a pre-construction pass in `load_config` so the loader
raises a genuine plain `ValueError` while the model stays un-constructible with dangling refs.
`pytest -v` from the project root: **2 passed**. Artifacts: the five files listed in the evidence
package above. Nothing is incomplete; no TODOs or stubs remain.

## FIXES APPLIED (round 1)

Round-1 verdict: **APPROVED**, with four MINOR findings. All four are now fixed. The two-layer
reference-check design was explicitly ruled a justified trade-off and is unchanged.

### F1 — MINOR — non-string `provider` / structurally invalid `providers` misreported as dangling

- **Change:** `src/gateway/config.py` — `_assert_raw_provider_references`.
  - `config.py:210-215`: the reference check now runs **only when the target's `provider` is a
    `str`** (`if not isinstance(provider, str): continue`). A missing/null/non-string `provider`
    falls through to `model_validate`, which reports the accurate required-field/type error.
  - `config.py:184-195`: when `providers` is present but **not** a mapping (e.g. a YAML list), the
    raw pass now `return`s early so `model_validate` reports the accurate structural error instead
    of a misleading `unknown provider … Known providers: []`. The omitted-section case
    (`providers` absent → `known_providers = []`) is preserved unchanged.
  - The genuine dangling **string** reference path is untouched: still a plain `ValueError` with
    the descriptive message (`config.py:216-219`).
- **Evidence (verbatim):**
  - `provider: [p1, p2]`:
    ```
    ValidationError
    1 validation error for AppConfig
    routes.r1.targets.0.provider
      Input should be a valid string [type=string_type, input_value=['p1', 'p2'], input_type=list]
    ```
    (no longer `Route 'r1' target references unknown provider '['p1', 'p2']'…`)
  - `providers:` as a list:
    ```
    ValidationError
    1 validation error for AppConfig
    providers
      Input should be a valid dictionary [type=dict_type, input_value=['p1'], input_type=list]
    ```

### F2 — MINOR — uncaught `TypeError` sorting mixed-type raw provider keys

- **Change:** `src/gateway/config.py:191` — the raw-pass sort now stringifies keys:
  `known_providers = sorted(str(key) for key in providers_raw)`. This cannot raise on heterogeneous
  keys, and the result is only used to build the human-readable message. This is the only raw-key
  sort in the codebase; the model-side `sorted(providers)` operates on validated `dict[str, …]`
  keys and is already total.
- **Evidence (verbatim):** providers with keys `1:` and `a:`, route targeting `p1`:
  ```
  EXC TYPE : builtins.ValueError
  plain ValueError? True
  EXC MSG  : Route 'r1' target references unknown provider 'p1'. Known providers: ['1', 'a'].
  ```
  No `TypeError` is raised. (With a target pointing at the string key `a`, the raw pass accepts and
  `model_validate` then reports the real problem accurately:
  `providers.1.[key] Input should be a valid string [type=string_type, input_value=1, input_type=int]`.)

### F3 — MINOR — Test 2 could not distinguish a plain `ValueError` from `ValidationError`

- **Change:** `tests/test_config.py` — added `from pydantic import ValidationError`, and in
  `test_unknown_provider_reference_raises` the raised exception is now pinned:
  ```python
  raised = excinfo.value
  assert type(raised) is ValueError, f"expected a plain ValueError, got {type(raised)!r}"
  assert not isinstance(raised, ValidationError)
  ```
  The existing message-content assertions (`coding_route`, `ghost_provider`, `sharellm_primary`)
  are kept unchanged.
- **Evidence (verbatim):** the strengthened test passes (see the suite output below), and the
  loader's dangling-ref error is `builtins.ValueError`, `plain ValueError? True`.

### F4 — MINOR — no regression guard for the documented defaults

- **Change:** `tests/test_config.py` — added `test_minimal_config_applies_documented_defaults`.
  It writes a minimal YAML to `tmp_path` that omits the entire `gateway:` block, `strategy` and
  `timeout_seconds` (while still declaring one valid provider and one route with a valid target),
  then asserts `host == "127.0.0.1"`, `port == 8000`, `log_level == "info"`,
  `strategy == "failover"`, `timeout_seconds == 15.0`, plus the declared types
  (`isinstance(port, int)`, `isinstance(timeout_seconds, float)`).
- **Evidence (verbatim):** the new test passes (see the suite output below).

### Verification after fixes

Command: `cd "C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway" && C:/Users/Notebook/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe -m pytest -v`

```
tests/test_config.py::test_example_config_loads_and_resolves_references PASSED [ 33%]
tests/test_config.py::test_unknown_provider_reference_raises PASSED      [ 66%]
tests/test_config.py::test_minimal_config_applies_documented_defaults PASSED [100%]

============================== 3 passed in 2.14s ==============================
```

Genuine dangling string reference unchanged (`type(exc) is ValueError == True`):

```
=== GENUINE dangling string ref ===
EXC TYPE : builtins.ValueError
plain ValueError? True
EXC MSG  : Route 'coding_route' target references unknown provider 'ghost_provider'. Known providers: ['sharellm_primary'].
```

`config.example.yaml` still loads cleanly:

```
port = 8000 | providers = ['openrouter_backup', 'sharellm_primary'] | routes = ['chat_route', 'coding_route']
coding order = ['sharellm_primary', 'openrouter_backup']
```

No existing assertion was weakened or removed. No files outside the project root were touched.
