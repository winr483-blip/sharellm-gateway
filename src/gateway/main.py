"""FastAPI application for the ShareLLM Smart Gateway.

Exposes the gateway's HTTP surface — ``/health``, ``/v1/models`` and
``/v1/chat/completions`` — and wires it to a single shared, pooled
:class:`httpx.AsyncClient` created once in the ASGI lifespan.

``/v1/chat/completions`` is served by the resilient failover engine
(:func:`gateway.engine.router.dispatch`): a route's ordered targets are tried in
turn, retryable failures (429/5xx, timeouts, network errors) fall over to the
next target, and recently failed targets are skipped via a per-application
:class:`~gateway.engine.router.CooldownRegistry` also created in the lifespan.

Around the engine the endpoint applies the ShareLLM adapter: the inbound payload
is run through the anti-422 parameter sanitizer before dispatch, upstream error
envelopes are normalized to the standard OpenAI types before they reach the
client, and a non-streaming success response carrying prompt-cache statistics
emits the structured ``[CACHE HIT] …`` line.

Importing this module is inert: it builds the application object but never
binds a socket, spawns a task or reads the configuration file. The server is
started only through :func:`run_cli`, which is also what ``python -m gateway``
invokes (see :mod:`gateway.__main__`).
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from gateway import __version__ as GATEWAY_VERSION
from gateway.adapters.sanitizer import sanitize_request_payload
from gateway.adapters.sharellm import (
    extract_cache_stats,
    log_cache_stats,
    sanitize_error_envelope,
)
from gateway.config import AppConfig, RouteConfig, load_config
from gateway.engine.proxy import JSONValue
from gateway.engine.router import CooldownRegistry, dispatch

__all__ = [
    "app",
    "create_app",
    "format_startup_banner",
    "parse_args",
    "resolve_config_path",
    "run_cli",
]

#: Pool limits for the shared upstream client (kept deliberately generous so
#: concurrent SSE streams do not serialise on connection acquisition).
_MAX_KEEPALIVE_CONNECTIONS = 50
_MAX_CONNECTIONS = 200

#: Fallback client timeout. Individual requests narrow this to the selected
#: target's ``timeout_seconds``.
_DEFAULT_TIMEOUT_SECONDS = 60.0

#: ``owned_by`` value reported for every aggregated model alias.
_MODEL_OWNED_BY = "sharellm-gateway"

#: Error ``type`` used for every gateway-generated OpenAI-style envelope.
_INVALID_REQUEST_ERROR = "invalid_request_error"

#: Configuration filename looked up in the working directory when ``--config``
#: is omitted.
_DEFAULT_CONFIG_FILENAME = "config.yaml"

#: Reference configuration used when ``config.yaml`` is absent. The repo ships
#: this file; ``config.yaml`` is gitignored because it holds real API keys, so
#: this fallback is the normal path for a fresh checkout.
_EXAMPLE_CONFIG_FILENAME = "config.example.yaml"

#: Width of the ASCII banner's horizontal rules.
_BANNER_RULE = "=" * 60


class _ReleasingStreamingResponse(StreamingResponse):
    """A ``StreamingResponse`` that always releases its body iterator.

    Starlette 1.8.0 never calls ``aclose()`` on ``body_iterator``: on client
    disconnect it cancels ``stream_response`` and abandons the iterator
    mid-iteration, leaving the async generator to be reclaimed by the GC
    finalizer. For a proxied upstream stream that defers the upstream
    ``aclose()`` past the disconnect — the spec forbids leaking an unclosed
    upstream socket — and it can raise on the generator's ``athrow`` path.

    Owning the iterator here closes it on *every* exit path — full drain, early
    stop or cancellation — before this coroutine returns.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            body_iterator = self.body_iterator
            if isinstance(body_iterator, AsyncGenerator):
                await body_iterator.aclose()


def _error_response(status_code: int, message: str) -> JSONResponse:
    """Build an OpenAI-style error response envelope."""
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": _INVALID_REQUEST_ERROR,
                "param": None,
                "code": None,
            }
        },
    )


def _resolve_route(config: AppConfig, model: str) -> RouteConfig | None:
    """Return the first route that serves ``model``, or ``None`` if unmapped."""
    for route in config.routes.values():
        if model in route.models:
            return route
    return None


def _normalize_error_body(status_code: int, body: JSONValue) -> JSONValue:
    """Normalize a ShareLLM error envelope before it reaches the client.

    Only error responses are considered, and only when the body is a JSON
    object; a non-``share_llm_error`` envelope — including the gateway's own
    ``gateway_failover_exhausted`` envelope — is returned unchanged (see
    :func:`gateway.adapters.sharellm.sanitize_error_envelope`).
    """
    if status_code < 400 or not isinstance(body, dict):
        return body
    return sanitize_error_envelope(status_code, body)


def _aggregate_models(config: AppConfig, created: int) -> list[dict[str, Any]]:
    """Flatten every route's model aliases into OpenAI-style model objects.

    Aliases are de-duplicated while preserving first-seen order, because the
    same alias may be served by more than one route.
    """
    data: list[dict[str, Any]] = []
    seen: set[str] = set()
    for route in config.routes.values():
        for alias in route.models:
            if alias in seen:
                continue
            seen.add(alias)
            data.append(
                {
                    "id": alias,
                    "object": "model",
                    "created": created,
                    "owned_by": _MODEL_OWNED_BY,
                }
            )
    return data


def create_app(config_path: str | Path | None = None) -> FastAPI:
    """Build the gateway application.

    Args:
        config_path: Explicit path to the YAML configuration to load at
            startup. When ``None`` (the production default) the lifespan calls
            ``load_config()``, which resolves ``config.yaml`` in the working
            directory. Tests pass ``config.example.yaml`` explicitly so the
            suite never depends on ``config.yaml`` existing.

    Returns:
        A configured, fully inert :class:`~fastapi.FastAPI` instance.
    """

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        config: AppConfig = (
            load_config(config_path) if config_path is not None else load_config()
        )
        http_client = httpx.AsyncClient(
            limits=httpx.Limits(
                max_keepalive_connections=_MAX_KEEPALIVE_CONNECTIONS,
                max_connections=_MAX_CONNECTIONS,
            ),
            timeout=httpx.Timeout(_DEFAULT_TIMEOUT_SECONDS),
        )
        application.state.app_config = config
        application.state.http_client = http_client
        application.state.cooldown_registry = CooldownRegistry()
        try:
            yield
        finally:
            await http_client.aclose()

    application = FastAPI(
        title="ShareLLM Smart Gateway",
        version=GATEWAY_VERSION,
        lifespan=lifespan,
    )

    @application.get("/health")
    async def health() -> dict[str, str]:
        """Liveness probe."""
        return {"status": "ok", "version": GATEWAY_VERSION}

    @application.get("/v1/models")
    async def list_models(request: Request) -> dict[str, Any]:
        """OpenAI-compatible model list aggregated from every configured route."""
        config: AppConfig = request.app.state.app_config
        return {
            "object": "list",
            "data": _aggregate_models(config, created=int(time.time())),
        }

    @application.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        """Proxy a chat-completions call to its route with resilient failover."""
        try:
            payload: object = await request.json()
        except ValueError:
            return _error_response(400, "Request body must be valid JSON.")

        if not isinstance(payload, dict):
            return _error_response(400, "Request body must be a JSON object.")

        model: object = payload.get("model")
        if not isinstance(model, str) or not model:
            return _error_response(
                400, "Missing required field 'model' in the request body."
            )

        config: AppConfig = request.app.state.app_config
        route = _resolve_route(config, model)
        if route is None:
            return _error_response(
                404,
                f"Model '{model}' is not mapped to any configured route. "
                f"Known models: {sorted({m for r in config.routes.values() for m in r.models})}.",
            )

        http_client: httpx.AsyncClient = request.app.state.http_client
        registry: CooldownRegistry = request.app.state.cooldown_registry
        headers = dict(request.headers)

        # Sanitize the *request* first (per-request, provider-agnostic), then
        # let the failover engine rewrite `model` per target (per-attempt). The
        # ordering matters: sanitizing after the rewrite would run the
        # anti-422 guard against a body the engine built, and the rewrite must
        # happen inside the engine's loop so each target gets its own model.
        sanitized_payload = sanitize_request_payload(payload)

        if bool(sanitized_payload.get("stream", False)):
            (
                status_code,
                error_body,
                response_headers,
                chunk_iterator,
            ), _provider_name = await dispatch(
                http_client, route, config, sanitized_payload, True, headers, registry
            )
            if chunk_iterator is None:
                # Every target failed (or the last one returned a retryable
                # error): report the final upstream status with the gateway's
                # failover envelope rather than committing an SSE stream.
                return JSONResponse(
                    status_code=status_code,
                    content=_normalize_error_body(status_code, error_body),
                    headers=response_headers,
                )
            return _ReleasingStreamingResponse(
                chunk_iterator,
                status_code=status_code,
                headers=response_headers,
                media_type="text/event-stream",
            )

        (
            status_code,
            body,
            response_headers,
        ), provider_name = await dispatch(
            http_client, route, config, sanitized_payload, False, headers, registry
        )
        body = _normalize_error_body(status_code, body)

        # Cache telemetry applies to non-streaming success responses only, and
        # is attributed to the provider that actually served the request.
        if status_code < 400 and provider_name is not None and isinstance(body, dict):
            cache_stats = extract_cache_stats(body)
            if cache_stats is not None:
                log_cache_stats(cache_stats, provider_name)

        return JSONResponse(
            status_code=status_code, content=body, headers=response_headers
        )

    return application


#: Module-level application instance. Building it performs no I/O and starts
#: nothing; the lifespan runs only when a server drives it.
app = create_app()


def _program_name(argv0: str | None = None) -> str:
    """Return the program name shown in the CLI's ``usage:`` line.

    Rule: take the basename of ``argv0`` (defaulting to ``sys.argv[0]``) so the
    command reports however it was actually invoked — the ``sharellm-gateway``
    console script names itself, rather than a fixed string. A trailing ``.exe``
    (the Windows console-script shim) is stripped. When the basename is not a
    meaningful command name it falls back to ``"gateway"``; the important case
    is ``python -m gateway``, where ``sys.argv[0]`` is the path to
    ``gateway/__main__.py`` and its basename (``__main__.py``) would be useless.

    Args:
        argv0: The value to derive the name from. Defaults to ``sys.argv[0]``.

    Returns:
        A non-empty program name suitable for ``argparse``'s ``prog=``.
    """
    raw = (sys.argv[0] if sys.argv else "") if argv0 is None else argv0
    name = Path(raw).name
    if name.lower().endswith(".exe"):
        name = name[:-4]
    if not name or name in {"__main__.py", "__main__", "-m", "-c"}:
        return "gateway"
    return name


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse the gateway's command-line arguments.

    Kept separate from :func:`run_cli` so argument handling can be exercised
    without starting a server. The ``usage:`` line's program name is derived
    from ``sys.argv[0]`` (see :func:`_program_name`), so
    ``sharellm-gateway --help`` reports ``sharellm-gateway`` while
    ``python -m gateway --help`` — whose ``sys.argv[0]`` is the path to
    ``gateway/__main__.py`` — reports ``gateway``.

    Args:
        argv: The argument vector **without** the program name (i.e.
            ``sys.argv[1:]``).

    Returns:
        The parsed namespace. ``config`` holds the explicit ``--config`` value,
        or ``None`` when the flag was not supplied.
    """
    parser = argparse.ArgumentParser(
        prog=_program_name(),
        description=(
            "ShareLLM Smart Gateway - a typed, multi-provider LLM routing "
            "gateway with automatic failover."
        ),
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help=(
            "Path to the YAML configuration file. Defaults to config.yaml, "
            "falling back to config.example.yaml when config.yaml is absent."
        ),
    )
    return parser.parse_args(argv)


def resolve_config_path(explicit: str | Path | None) -> Path:
    """Resolve which configuration file the CLI should load.

    Precedence:

    1. An explicit ``--config`` path is used verbatim and must exist.
    2. Otherwise ``config.yaml`` in the working directory, when present.
    3. Otherwise ``config.example.yaml`` in the working directory.
    4. Otherwise a :class:`FileNotFoundError` naming both candidates is raised.

    Args:
        explicit: The value of ``--config``, or ``None`` when it was omitted.

    Returns:
        The path of the configuration file to load.

    Raises:
        FileNotFoundError: If an explicit path does not point at a file, or if
            neither ``config.yaml`` nor ``config.example.yaml`` exists.
    """
    if explicit is not None:
        explicit_path = Path(explicit)
        if not explicit_path.is_file():
            raise FileNotFoundError(
                f"Configuration file not found: {explicit_path.resolve()} "
                f"(from --config)."
            )
        return explicit_path

    default_path = Path(_DEFAULT_CONFIG_FILENAME)
    if default_path.is_file():
        return default_path

    example_path = Path(_EXAMPLE_CONFIG_FILENAME)
    if example_path.is_file():
        return example_path

    raise FileNotFoundError(
        f"No configuration file found. Expected either {default_path.resolve()} "
        f"or {example_path.resolve()} in the working directory (pass --config "
        f"PATH to point at another file)."
    )


def format_startup_banner(config: AppConfig) -> str:
    """Render the CLI startup banner for ``config``.

    Pure: it returns the banner text and never prints, so it is directly
    testable. The banner is ASCII-only (no box-drawing characters, no emoji)
    and shows the gateway's OpenAI-compatible base URL, every active route with
    its ordered fallback targets, and a Cursor/Cline setup tip.

    Args:
        config: The loaded gateway configuration.

    Returns:
        The banner text, newline-separated (no trailing newline).
    """
    host = config.gateway.host
    port = config.gateway.port
    base_url = f"http://{host}:{port}/v1"

    lines: list[str] = [
        _BANNER_RULE,
        f" ShareLLM Smart Gateway {GATEWAY_VERSION}",
        _BANNER_RULE,
        f" Gateway URL  : {base_url}",
        f" Health check : http://{host}:{port}/health",
        "",
        f" Active routes ({len(config.routes)}):",
    ]

    for route_name, route in config.routes.items():
        lines.append(f"   - {route_name}  (strategy: {route.strategy})")
        lines.append(f"       models : {', '.join(route.models)}")
        for index, target in enumerate(route.targets, start=1):
            lines.append(
                f"       {index}. {target.provider}  ->  {target.model}"
                f"  (timeout {target.timeout_seconds:g}s)"
            )

    lines.extend(
        [
            "",
            " Cursor / Cline setup:",
            f"   Set Base URL to {base_url}",
            _BANNER_RULE,
        ]
    )
    return "\n".join(lines)


def run_cli(argv: Sequence[str] | None = None) -> None:
    """Entry point for ``python -m gateway``: parse args, load config, serve.

    Prints the startup banner, then hands the application to uvicorn. This is
    the only code path that binds a socket; importing :mod:`gateway.main` never
    reaches it.

    Args:
        argv: The argument vector without the program name. Defaults to
            ``sys.argv[1:]`` when ``None``.
    """
    args = parse_args(sys.argv[1:] if argv is None else argv)
    config_path = resolve_config_path(args.config)
    config: AppConfig = load_config(config_path)
    print(format_startup_banner(config), flush=True)
    uvicorn.run(
        create_app(config_path),
        host=config.gateway.host,
        port=config.gateway.port,
        log_level=config.gateway.log_level,
    )


if __name__ == "__main__":
    run_cli()
