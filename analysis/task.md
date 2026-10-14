# Task 1.1 — Repository Scaffold & Typed Config Engine

Project: **ShareLLM Smart Gateway** (`sharellm-gateway`)
Project root: `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway`

## Objective

Create the foundation of the repository and implement a robust, strictly typed
configuration loader using Pydantic v2 and PyYAML.

## Scope of work

### 1. Directory layout

```
sharellm-gateway/
├── .gitignore
├── pyproject.toml
├── config.example.yaml
├── src/
│   └── gateway/
│       ├── __init__.py
│       ├── config.py
│       ├── engine/
│       │   └── __init__.py
│       ├── adapters/
│       │   └── __init__.py
│       └── utils/
│           └── __init__.py
└── tests/
    ├── __init__.py
    └── test_config.py
```

### 2. `.gitignore`

Standard Python ignore set: `__pycache__`, `.venv`, `.env`, `config.yaml`, `*.json`,
plus the usual build/dist/pytest-cache/tooling artifacts.

### 3. `pyproject.toml`

- Project name `sharellm-gateway`, version `0.1.0`, `requires-python = ">=3.11"`.
- Core dependencies:
  `fastapi>=0.115.0`, `uvicorn[standard]>=0.32.0`, `httpx>=0.27.2`,
  `pydantic>=2.9.0`, `pydantic-settings>=2.5.0`, `pyyaml>=6.0.2`.
- Dev dependencies (extra group named `dev`):
  `pytest>=8.3.0`, `pytest-asyncio>=0.24.0`, `respx>=0.21.1`.
- src-layout package discovery so `src/gateway` is importable.

### 4. `config.example.yaml`

Must support defining providers and multi-target failover routes.

- `gateway`: `host` (default `127.0.0.1`), `port` (default `8000`), `log_level`.
- `providers`: key-value map (e.g. `sharellm_primary`, `openrouter_backup`) with
  `type` (`"sharellm"` | `"openai"`), `base_url`, `api_key`.
- `routes`: key-value map (e.g. `coding_route`) with:
  - `models`: list of model alias strings matching this route
    (e.g. `["gpt-6.1-sol", "gpt-6-astra"]`).
  - `strategy`: string, default `"failover"`.
  - `targets`: **ordered** list of target dicts containing:
    - `provider` — reference to a `providers` key
    - `model` — upstream model id
    - `timeout_seconds` — float, default `15.0`

### 5. `src/gateway/config.py`

- Pydantic v2 (`pydantic.BaseModel`, `Field`).
- Models: `ProviderConfig`, `RouteTargetConfig`, `RouteConfig`, `GatewayServerConfig`,
  `AppConfig`.
- `load_config(path: str | Path = "config.yaml") -> AppConfig`:
  - Reads YAML via `yaml.safe_load`.
  - Validates that every `target.provider` in `routes` exists in the `providers` map;
    fail fast with a descriptive `ValueError` if the reference is broken.
  - Returns the validated `AppConfig`.

### 6. `tests/test_config.py`

- Test 1: `config.example.yaml` loads into `AppConfig` successfully and validates
  target references.
- Test 2: a route referencing a non-existent provider raises a validation error.

## Constraints & coding standards

- Strictly typed: type hints on all functions and attributes.
- No mock comments, no `TODO` placeholders — complete, working code only.
- Deterministic execution: no background processes or loops.
- Tests must pass under the isolated venv:
  `C:/Users/Notebook/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe`

## Definition of done

- All files above exist with complete implementations.
- `pytest` passes from the project root.
- `config.example.yaml` round-trips through `load_config` into a valid `AppConfig`.
