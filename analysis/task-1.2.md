# Task 1.2 — Core Transparent Reverse Proxy & Streaming Engine

Project: **ShareLLM Smart Gateway** (`sharellm-gateway`)
Project root: `C:/Users/Notebook/WorkBuddy AI/2026-10-14-19-08-04/sharellm-gateway`

Task 1.1 (Repository Scaffold & Typed Config) is verified and merged. This task builds the
core reverse proxy engine on top of it.

## Objective

Implement the Core Reverse Proxy Engine using FastAPI and HTTPX, with high-performance
connection pooling, full SSE (Server-Sent Events) streaming pass-through, and standard
endpoints.

## Scope of work

### 1. `src/gateway/engine/proxy.py`

- Define a proxy forwarding engine:
  `forward_request(client: httpx.AsyncClient, target: RouteTargetConfig, provider: ProviderConfig, request_data: dict, stream: bool, headers: dict)`.
- **Header sanitation:** strip hop-by-hop headers (`host`, `content-length`, `connection`),
  set `Authorization: Bearer <provider.api_key>`.
- **Non-streaming flow:** forward the request, await the JSON response, return
  `(status_code, response_json, headers)`.
- **Streaming flow:** forward with `stream=True`, yield raw SSE chunks via an async byte
  generator (`response.aiter_bytes()`), ensuring the upstream response context manager is
  cleanly closed upon stream termination.

### 2. `src/gateway/main.py`

- FastAPI application with a Lifespan context manager (`@asynccontextmanager`):
  - **Startup:** load `AppConfig` via `load_config()`, initialize a shared persistent
    `httpx.AsyncClient` with keep-alive limits
    (`limits=httpx.Limits(max_keepalive_connections=50, max_connections=200)`),
    store on `app.state`.
  - **Shutdown:** await `app.state.http_client.aclose()`.
- `GET /health` → `{"status": "ok", "version": "0.1.0"}` (200 OK).
- `GET /v1/models` → OpenAI-compatible model list (`{"object": "list", "data": [...]}`)
  built from all models defined in `app_config.routes`.
- `POST /v1/chat/completions`:
  - Accepts a standard OpenAI payload.
  - Resolves the requested `model` against `app_config.routes`. If unmapped, returns HTTP
    **404** with an OpenAI-style error envelope.
  - Picks the primary target (`route.targets[0]`) and its associated provider.
  - `stream=True` → `starlette.responses.StreamingResponse` with media type `text/event-stream`.
  - `stream=False` → standard JSON response.
- Entrypoint runner `run_cli()` using `uvicorn.run`, honoring `gateway` host and port from config.

### 3. `tests/test_proxy.py`

Use `pytest-asyncio`, `respx`, and `httpx.ASGITransport` to test without opening real
network ports.

- Test 1: `GET /health` returns 200 OK.
- Test 2: `GET /v1/models` returns the aggregated models defined in `config.example.yaml`.
- Test 3: Non-streaming `POST /v1/chat/completions` matches the mock upstream response.
- Test 4: Streaming `POST /v1/chat/completions` correctly forwards SSE chunks line-by-line.
- Test 5: Requesting an unmapped model returns 404 with
  `{"error": {"message": ..., "type": "invalid_request_error"}}`.

## Constraints & coding standards

- **Zero-copy / low-overhead streaming:** do NOT buffer the entire stream into memory.
- **Context safety:** upstream HTTP streams must never leak unclosed sockets on client
  disconnection.
- Strict type annotations (mypy compliant). No `TODO` comments.
- Run the full test suite (`pytest`) and ensure all tests pass — both the config suite from
  Task 1.1 and the new proxy suite.

## Definition of done

- `src/gateway/engine/proxy.py`, `src/gateway/main.py` and `tests/test_proxy.py` exist with
  complete implementations.
- `pytest` passes from the project root: the three Task 1.1 config tests plus the five new
  proxy tests.
- No real network ports are opened by the test suite.
