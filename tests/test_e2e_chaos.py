"""Capstone end-to-end chaos journey, CLI tests and Milestone 1 freeze (Task 1.5).

Task 1.5 adds this one new test module (the task's file list creates no other
test file). It covers:

* the **end-to-end chaos journey** — a realistic Cursor coding session driven
  through the real ASGI application (:func:`gateway.main.create_app`) with the
  upstream providers intercepted by :mod:`respx`. The journey has two legs, as
  the task spec requires: a streaming failover (leg A) and a cached,
  non-streaming follow-up (leg B);
* the **F2 word-boundary regression guard** for the rate-limit matcher;
* the **CLI** pure-function tests (``parse_args`` / ``resolve_config_path`` /
  ``format_startup_banner``) plus one ``run_cli`` test that monkeypatches
  ``uvicorn.run`` so no port is ever bound;
* a **zero-leak** client-disconnect check for the failover path.

Everything runs in-process: the gateway is driven through
:class:`httpx.ASGITransport` (no socket is bound) and the upstreams are mocked
by :mod:`respx`, so no real network call is ever made. Upstream URLs, the
banner URL, model ids and provider names are derived from
``config.example.yaml`` rather than hard-coded; the only literals are the
inbound model alias, the in-process client's base URL, and a few
reference-config model ids used as extra cross-checks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import pytest
import pytest_asyncio
import respx
import uvicorn
from fastapi import FastAPI

from gateway.adapters.sharellm import SHARE_LLM_ERROR_TYPE, sanitize_error_envelope
from gateway.config import AppConfig, load_config
from gateway.engine.router import CooldownRegistry
from gateway.main import (
    create_app,
    format_startup_banner,
    parse_args,
    resolve_config_path,
    run_cli,
)

EXAMPLE_CONFIG_PATH: Path = Path(__file__).resolve().parents[1] / "config.example.yaml"

#: The project root and its ``src`` directory, used to drive the real CLI in a
#: child process deterministically (see the subprocess CLI test).
PROJECT_ROOT: Path = EXAMPLE_CONFIG_PATH.parent
SRC_DIR: Path = PROJECT_ROOT / "src"

#: The reference configuration, loaded once for the whole module.
EXAMPLE_CONFIG: AppConfig = load_config(EXAMPLE_CONFIG_PATH)

#: ``coding_route`` and its two ordered targets (primary, backup).
CODING_ROUTE = EXAMPLE_CONFIG.routes["coding_route"]
PRIMARY_TARGET = CODING_ROUTE.targets[0]
BACKUP_TARGET = CODING_ROUTE.targets[1]

PRIMARY_PROVIDER = EXAMPLE_CONFIG.providers[PRIMARY_TARGET.provider]
BACKUP_PROVIDER = EXAMPLE_CONFIG.providers[BACKUP_TARGET.provider]

#: The chat-completions endpoints the gateway must POST to, derived from config.
PRIMARY_URL = PRIMARY_PROVIDER.base_url.rstrip("/") + "/chat/completions"
BACKUP_URL = BACKUP_PROVIDER.base_url.rstrip("/") + "/chat/completions"

GATEWAY_BASE_URL = "http://gateway.test"

#: The OpenAI-compatible base URL the startup banner must advertise, derived
#: from the configured gateway host/port (the banner builds it as
#: ``http://<host>:<port>/v1``). For ``config.example.yaml`` this is
#: ``http://127.0.0.1:8000/v1``.
EXPECTED_BASE_URL = (
    f"http://{EXAMPLE_CONFIG.gateway.host}:{EXAMPLE_CONFIG.gateway.port}/v1"
)

#: The alias the developer's editor asks for. It is served by ``coding_route``.
MODEL_ALIAS = "gpt-6.1-sol"

#: The message list every forwarded body must carry through intact.
MESSAGES: list[dict[str, str]] = [
    {"role": "system", "content": "You are a helpful coding assistant."},
    {"role": "user", "content": "Refactor this function."},
]

#: The streaming request the editor sends. It carries the *empty* tool fields
#: the anti-422 guard exists to strip (``tools: []`` and ``tool_choice:
#: "none"``), so a leak is unmistakable in target 2's captured body.
STREAMING_INBOUND: dict[str, Any] = {
    "model": MODEL_ALIAS,
    "messages": MESSAGES,
    "tools": [],
    "tool_choice": "none",
    "stream": True,
}

#: The body target 2 must actually receive for leg A: the tool fields gone, the
#: messages intact and the model rewritten to the backup target's identifier.
LEG_A_EXPECTED_BACKUP_BODY: dict[str, Any] = {
    "model": BACKUP_TARGET.model,
    "messages": MESSAGES,
    "stream": True,
}

#: The ShareLLM-shaped 429 target 1 answers with.
PRIMARY_429_BODY: dict[str, Any] = {
    "error": {
        "message": "channel rate limit exceeded",
        "type": SHARE_LLM_ERROR_TYPE,
    }
}

#: A multi-chunk SSE stream (with an embedded cache-details usage chunk) that
#: target 2 returns for leg A. The gateway relays it verbatim; it never parses
#: the SSE body (see the module docstring of ``gateway.main``).
BACKUP_SSE_CHUNKS: list[bytes] = [
    b'data: {"id":"cmpl-astra","choices":[{"delta":{"content":"Hel"}}]}\n\n',
    b'data: {"id":"cmpl-astra","choices":[{"delta":{"content":"lo"}}]}\n\n',
    (
        b'data: {"id":"cmpl-astra","choices":[],"usage":{"prompt_tokens":4387,'
        b'"prompt_tokens_details":{"cached_tokens":3840}}}\n\n'
    ),
    b"data: [DONE]\n\n",
]

#: The cached, non-streaming body target 2 returns for leg B. It carries
#: ``usage.prompt_tokens_details.cached_tokens``, so the ``[CACHE HIT] …`` line
#: is emitted and attributed to the provider that actually served it.
BACKUP_CACHED_BODY: dict[str, Any] = {
    "id": "chatcmpl-astra",
    "object": "chat.completion",
    "model": BACKUP_TARGET.model,
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Done."},
            "finish_reason": "stop",
        }
    ],
    "usage": {
        "prompt_tokens": 4387,
        "completion_tokens": 3,
        "total_tokens": 4390,
        "prompt_tokens_details": {"cached_tokens": 3840},
    },
}

#: The exact ``[CACHE HIT] …`` line leg B must emit for the body above.
EXPECTED_CACHE_LINE = (
    f"[CACHE HIT] 3840/4387 tokens (87.5%) via {BACKUP_TARGET.provider}"
)


class RecordingByteStream(httpx.AsyncByteStream):
    """Upstream body stream that records release order and close count.

    ``close_count`` proves a stream is released **exactly once** — a leaked
    stream is never closed, and a double-close would show a count of two.
    """

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.released: list[bytes] = []
        self.closed = False
        self.close_count = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            self.released.append(chunk)
            yield chunk

    async def aclose(self) -> None:
        self.closed = True
        self.close_count += 1


class _RecordCollector(logging.Handler):
    """A logging handler that keeps the raw :class:`LogRecord` objects."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@dataclass
class GatewayHarness:
    """A lifespan-started gateway app plus a client wired to its ASGI surface."""

    app: FastAPI
    client: httpx.AsyncClient


@pytest_asyncio.fixture
async def gateway() -> AsyncIterator[GatewayHarness]:
    """Start the gateway's lifespan and yield an in-process ASGI client.

    ``httpx.ASGITransport`` does not run the ASGI lifespan, so it is driven
    explicitly with ``app.router.lifespan_context(app)`` — exactly as the
    existing suites do. That guarantees ``app.state.http_client``,
    ``app.state.app_config`` and ``app.state.cooldown_registry`` exist before
    the first request.
    """
    application = create_app(EXAMPLE_CONFIG_PATH)
    async with application.router.lifespan_context(application):
        assert isinstance(application.state.http_client, httpx.AsyncClient)
        assert isinstance(application.state.cooldown_registry, CooldownRegistry)
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport, base_url=GATEWAY_BASE_URL
        ) as client:
            yield GatewayHarness(app=application, client=client)


# --------------------------------------------------------------------------- #
# The end-to-end chaos journey (legs A and B)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_chaos_journey_streaming_failover_then_cached_follow_up(
    gateway: GatewayHarness,
) -> None:
    """A full Cursor session: streaming failover, then a cached follow-up.

    **Leg A** — the editor streams ``POST /v1/chat/completions`` for
    ``gpt-6.1-sol`` with a (empty) tools schema. Target 1 answers ``429`` with
    a ShareLLM error; the gateway prevents a client disconnect, fails over and
    relays target 2's ``200`` SSE stream unbroken.

    **Leg B** — a second, non-streaming request in the same session. Target 1
    is now cooling down, so it is routed straight to target 2, whose body
    carries prompt-cache details; the gateway emits the ``[CACHE HIT] …`` line.

    Assertions are made against the *captured upstream requests* (what target 1
    and target 2 actually received) as well as the client-visible responses.
    """
    registry: CooldownRegistry = gateway.app.state.cooldown_registry
    # Start from a clean registry so the test cannot become order-dependent.
    registry.reset()

    created_backup_streams: list[RecordingByteStream] = []

    def backup_responder(request: httpx.Request) -> httpx.Response:
        """Answer target 2: an SSE stream for leg A, a cached JSON body for leg B."""
        body = json.loads(request.content)
        if body.get("stream"):
            stream = RecordingByteStream(BACKUP_SSE_CHUNKS)
            created_backup_streams.append(stream)
            return httpx.Response(
                200, stream=stream, headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(
            200, json=BACKUP_CACHED_BODY, headers={"content-type": "application/json"}
        )

    sharellm_logger = logging.getLogger("gateway.adapters.sharellm")
    previous_level = sharellm_logger.level
    collector = _RecordCollector()
    sharellm_logger.setLevel(logging.INFO)
    sharellm_logger.addHandler(collector)
    try:
        with respx.mock(assert_all_called=False) as mock:
            primary = mock.post(PRIMARY_URL).mock(
                return_value=httpx.Response(
                    429,
                    json=PRIMARY_429_BODY,
                    headers={"content-type": "application/json"},
                )
            )
            backup = mock.post(BACKUP_URL).mock(side_effect=backup_responder)

            # --- Leg A: streaming failover --------------------------------- #
            async with gateway.client.stream(
                "POST", "/v1/chat/completions", json=STREAMING_INBOUND
            ) as response:
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                streamed = await response.aread()

            # --- Leg B: cached, non-streaming follow-up -------------------- #
            follow_up = await gateway.client.post(
                "/v1/chat/completions",
                json={
                    "model": MODEL_ALIAS,
                    "messages": [{"role": "user", "content": "Now the tests."}],
                    "stream": False,
                },
            )

            # --- What each upstream actually received ---------------------- #
            primary_bodies = [json.loads(call.request.content) for call in primary.calls]
            backup_bodies = [json.loads(call.request.content) for call in backup.calls]
            primary_call_count = primary.call_count
            backup_call_count = backup.call_count
    finally:
        sharellm_logger.removeHandler(collector)
        sharellm_logger.setLevel(previous_level)

    # -- Verification 1: the client saw 200 and an unbroken SSE stream ----- #
    assert streamed == b"".join(BACKUP_SSE_CHUNKS)
    assert [event for event in streamed.split(b"\n\n") if event] == [
        chunk.removesuffix(b"\n\n") for chunk in BACKUP_SSE_CHUNKS
    ]
    assert follow_up.status_code == 200
    assert follow_up.json() == BACKUP_CACHED_BODY

    # The fully-drained leg A stream was released exactly once.
    assert len(created_backup_streams) == 1
    assert created_backup_streams[0].closed is True
    assert created_backup_streams[0].close_count == 1

    # -- Verification 2: the sanitizer cleaned the tool fields pre-upstream  #
    assert primary_call_count == 1
    assert backup_call_count == 2

    # Target 1 saw the alias as-is (the alias *is* the primary target's model).
    assert len(primary_bodies) == 1
    assert primary_bodies[0]["model"] == PRIMARY_TARGET.model == "gpt-6.1-sol"
    assert primary_bodies[0]["model"] != BACKUP_TARGET.model

    # Target 2's leg A body: model rewritten, tool fields stripped, rest intact.
    leg_a_body = backup_bodies[0]
    assert leg_a_body == LEG_A_EXPECTED_BACKUP_BODY
    assert leg_a_body["model"] == BACKUP_TARGET.model == "gpt-6-astra"
    assert "tools" not in leg_a_body
    assert "tool_choice" not in leg_a_body
    assert leg_a_body["messages"] == MESSAGES
    assert set(leg_a_body) == {"model", "messages", "stream"}

    # -- Verification 3: the cache telemetry line was emitted for leg B ----- #
    cache_lines = [
        record.getMessage()
        for record in collector.records
        if "[CACHE HIT]" in record.getMessage()
    ]
    assert cache_lines == [EXPECTED_CACHE_LINE]
    assert all(record.levelno == logging.INFO for record in collector.records)

    # -- Verification 4: target 1 cooled down; target 2 served the follow-up  #
    assert backup_bodies[1]["model"] == BACKUP_TARGET.model
    assert registry.is_cooling(PRIMARY_TARGET) is True
    assert registry.is_cooling(BACKUP_TARGET) is False
    # Across the whole journey: target 1 called exactly once (leg A's failover
    # attempt) and target 2 exactly twice (leg A's failover + leg B direct).
    assert primary_call_count == 1
    assert backup_call_count == 2


# --------------------------------------------------------------------------- #
# F2 — the rate-limit match is word-bounded
# --------------------------------------------------------------------------- #


def test_rate_limit_match_is_word_bounded() -> None:
    """F2: an unanchored ``"limit"`` substring must not fake a rate limit.

    ``"unlimited tokens available"`` contains the letters ``limit`` but not the
    *word* ``limit``, so a 400 carrying it must map to ``invalid_request_error``
    — not ``rate_limit_error``. A genuine ``"rate limit exceeded"`` still maps
    to ``rate_limit_error`` (precedence unchanged).
    """
    unanchored = {
        "error": {"message": "unlimited tokens available", "type": SHARE_LLM_ERROR_TYPE}
    }
    assert sanitize_error_envelope(400, unanchored) == {
        "error": {
            "message": "unlimited tokens available",
            "type": "invalid_request_error",
        }
    }

    genuine = {"error": {"message": "rate limit exceeded", "type": SHARE_LLM_ERROR_TYPE}}
    assert sanitize_error_envelope(400, genuine) == {
        "error": {"message": "rate limit exceeded", "type": "rate_limit_error"}
    }

    # The plural form is still a rate-limit signal, and a 5xx without any
    # rate-limit wording still maps to api_error (precedence unchanged).
    assert sanitize_error_envelope(
        400, {"error": {"message": "too many limits", "type": SHARE_LLM_ERROR_TYPE}}
    )["error"]["type"] == "rate_limit_error"
    assert sanitize_error_envelope(
        500, {"error": {"message": "boom", "type": SHARE_LLM_ERROR_TYPE}}
    )["error"]["type"] == "api_error"


@pytest.mark.asyncio
async def test_rate_limit_word_boundary_guard_end_to_end(
    gateway: GatewayHarness,
) -> None:
    """A 400 ``"unlimited tokens available"`` reaches the client un-mislabelled.

    Driven through the real app: the 400 is non-retryable, so it is relayed
    immediately (target 2 is never consulted) and normalized to
    ``invalid_request_error`` before the client sees it.
    """
    registry: CooldownRegistry = gateway.app.state.cooldown_registry
    registry.reset()

    with respx.mock(assert_all_called=False) as mock:
        primary = mock.post(PRIMARY_URL).mock(
            return_value=httpx.Response(
                400,
                json={
                    "error": {
                        "message": "unlimited tokens available",
                        "type": SHARE_LLM_ERROR_TYPE,
                    }
                },
                headers={"content-type": "application/json"},
            )
        )
        backup = mock.post(BACKUP_URL).mock(
            return_value=httpx.Response(200, json=BACKUP_CACHED_BODY)
        )

        response = await gateway.client.post(
            "/v1/chat/completions",
            json={
                "model": MODEL_ALIAS,
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": False,
            },
        )

        assert primary.call_count == 1
        assert backup.call_count == 0  # 400 is non-retryable: no failover

    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "message": "unlimited tokens available",
            "type": "invalid_request_error",
        }
    }


# --------------------------------------------------------------------------- #
# Zero-leak — a client disconnect releases every upstream stream at once
# --------------------------------------------------------------------------- #


def _streaming_scope(request_body: bytes) -> dict[str, Any]:
    """Build the ASGI HTTP scope for a streaming chat-completions POST."""
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(request_body)).encode()),
        ],
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
        "scheme": "http",
    }


@pytest.mark.asyncio
async def test_client_disconnect_during_failover_releases_every_stream(
    gateway: GatewayHarness,
) -> None:
    """A disconnect while relaying target 2's stream releases *every* stream.

    The attempt sequence is: target 1 answers 429 (its error body is consumed
    and released inside the failover engine) → target 2 opens a 200 SSE stream
    whose first chunk is buffered by the TTFT inspection → the caller
    disconnects while the response layer is parked on that buffered chunk.

    The assertions are made **synchronously**, before any event-loop turn could
    run a GC finalizer, so they prove release happened *at disconnect time* —
    each stream closed exactly once, with no unhandled loop exceptions.
    """
    runs = 10
    request_body = json.dumps(STREAMING_INBOUND).encode()

    registry: CooldownRegistry = gateway.app.state.cooldown_registry

    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop_errors: list[str] = []

    def _record_loop_error(
        _loop: asyncio.AbstractEventLoop, context: dict[str, Any]
    ) -> None:
        exception = context.get("exception")
        if exception is not None and not isinstance(exception, asyncio.CancelledError):
            loop_errors.append(f"{type(exception).__name__}: {exception}")

    loop.set_exception_handler(_record_loop_error)
    try:
        for _ in range(runs):
            # Each run must exercise the full attempt sequence, so the previous
            # run's cooldown on target 1 is cleared first.
            registry.reset()

            primary_stream = RecordingByteStream([json.dumps(PRIMARY_429_BODY).encode()])
            backup_stream = RecordingByteStream(BACKUP_SSE_CHUNKS)

            with respx.mock(assert_all_called=False) as mock:
                mock.post(PRIMARY_URL).mock(
                    return_value=httpx.Response(
                        429,
                        stream=primary_stream,
                        headers={"content-type": "application/json"},
                    )
                )
                mock.post(BACKUP_URL).mock(
                    return_value=httpx.Response(
                        200,
                        stream=backup_stream,
                        headers={"content-type": "text/event-stream"},
                    )
                )

                first_body_sent = asyncio.Event()
                stalled_client = asyncio.Event()  # never set: the client stops reading
                request_delivered = False

                async def receive() -> dict[str, Any]:
                    nonlocal request_delivered
                    if not request_delivered:
                        request_delivered = True
                        return {
                            "type": "http.request",
                            "body": request_body,
                            "more_body": False,
                        }
                    await first_body_sent.wait()
                    return {"type": "http.disconnect"}

                async def send(message: dict[str, Any]) -> None:
                    if message["type"] == "http.response.body" and message.get("body"):
                        first_body_sent.set()
                        await stalled_client.wait()

                async with asyncio.timeout(5):
                    await gateway.app(_streaming_scope(request_body), receive, send)

            # Synchronous assertions: no await, no gc.collect().
            assert primary_stream.closed is True, "failed attempt's stream leaked"
            assert backup_stream.closed is True, "backup stream leaked past disconnect"
            assert primary_stream.close_count == 1, "failed attempt's stream closed twice"
            assert backup_stream.close_count == 1, "backup stream closed twice"
            # Only the buffered first chunk was read ahead — zero-copy holds.
            assert backup_stream.released == [BACKUP_SSE_CHUNKS[0]]
    finally:
        loop.set_exception_handler(previous_handler)

    assert loop_errors == []


# --------------------------------------------------------------------------- #
# CLI — pure functions (no server is ever started)
# --------------------------------------------------------------------------- #


def test_parse_args_defaults_config_to_none() -> None:
    """With no flags, ``config`` is ``None`` so the fallback resolution runs."""
    args = parse_args([])

    assert args.config is None


def test_parse_args_reads_explicit_config() -> None:
    """``--config PATH`` is captured verbatim."""
    args = parse_args(["--config", "custom/settings.yaml"])

    assert args.config == "custom/settings.yaml"


def test_parse_args_help_exits_without_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--help`` prints usage and raises ``SystemExit(0)`` (argparse behaviour).

    ``sys.argv[0]`` is pinned so the ``usage:`` program name is deterministic
    regardless of how the test runner itself was invoked (see ``_program_name``).
    """
    monkeypatch.setattr(sys, "argv", ["gateway"])

    with pytest.raises(SystemExit) as excinfo:
        parse_args(["--help"])

    assert excinfo.value.code == 0
    assert "usage: gateway" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("argv0", "expected_prog"),
    [
        # The console script names itself (with or without a path / .exe shim).
        ("sharellm-gateway", "sharellm-gateway"),
        ("/usr/local/bin/sharellm-gateway", "sharellm-gateway"),
        ("C:/venv/Scripts/sharellm-gateway.exe", "sharellm-gateway"),
        # `python -m gateway`: argv[0] is the path to __main__.py, whose basename
        # is not a useful command name, so the fallback applies.
        ("C:/proj/src/gateway/__main__.py", "gateway"),
        ("", "gateway"),
    ],
)
def test_parse_args_program_name_reflects_invocation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv0: str,
    expected_prog: str,
) -> None:
    """The ``usage:`` line names the command as it was actually invoked (NIT-1)."""
    monkeypatch.setattr(sys, "argv", [argv0])

    with pytest.raises(SystemExit):
        parse_args(["--help"])

    assert capsys.readouterr().out.startswith(f"usage: {expected_prog} ")


def test_resolve_config_path_uses_explicit_path(tmp_path: Path) -> None:
    """An explicit path that exists is returned as-is."""
    explicit = tmp_path / "custom.yaml"
    explicit.write_text("gateway: {}\n", encoding="utf-8")

    assert resolve_config_path(explicit) == explicit
    assert resolve_config_path(str(explicit)) == explicit


def test_resolve_config_path_rejects_missing_explicit_path(tmp_path: Path) -> None:
    """An explicit path that does not exist raises a clear error naming it."""
    missing = tmp_path / "nope.yaml"

    with pytest.raises(FileNotFoundError) as excinfo:
        resolve_config_path(missing)

    assert "nope.yaml" in str(excinfo.value)
    assert "--config" in str(excinfo.value)


def test_resolve_config_path_prefers_config_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``config.yaml`` wins over ``config.example.yaml`` when both exist."""
    (tmp_path / "config.yaml").write_text("gateway: {}\n", encoding="utf-8")
    (tmp_path / "config.example.yaml").write_text("gateway: {}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert resolve_config_path(None) == Path("config.yaml")


def test_resolve_config_path_falls_back_to_example(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no ``config.yaml``, the example configuration is the fallback."""
    (tmp_path / "config.example.yaml").write_text("gateway: {}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert resolve_config_path(None) == Path("config.example.yaml")


def test_resolve_config_path_raises_when_neither_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With neither file present, the error names both candidates."""
    monkeypatch.chdir(tmp_path)

    with pytest.raises(FileNotFoundError) as excinfo:
        resolve_config_path(None)

    message = str(excinfo.value)
    assert "config.yaml" in message
    assert "config.example.yaml" in message


def test_format_startup_banner_is_pure_ascii_and_informative(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The banner is ASCII-only, shows the URL, routes/targets and the tip.

    Purity is locked here, not merely by source inspection: both calls below
    must return the text and write **nothing** to stdout or stderr.
    """
    banner = format_startup_banner(EXAMPLE_CONFIG)

    # ASCII only: no box-drawing characters, no emoji.
    assert banner.isascii()

    # The OpenAI-compatible base URL, derived from the configured host/port.
    assert EXPECTED_BASE_URL in banner
    # The Cursor/Cline tip points at that same endpoint.
    assert f"Set Base URL to {EXPECTED_BASE_URL}" in banner

    # Every active route is listed with its ordered fallback targets.
    for route_name, route in EXAMPLE_CONFIG.routes.items():
        assert route_name in banner
        for target in route.targets:
            assert target.provider in banner
            assert target.model in banner

    # Pure: same input, same output...
    assert format_startup_banner(EXAMPLE_CONFIG) == banner

    # ...and neither call printed anything.
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_run_cli_prints_banner_without_binding_a_port(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``run_cli`` prints the banner and hands the app to uvicorn.

    ``uvicorn.run`` is monkeypatched, so this test never binds a socket.
    """
    run_calls: list[dict[str, Any]] = []

    def fake_uvicorn_run(app: Any, **kwargs: Any) -> None:
        run_calls.append({"app": app, **kwargs})

    monkeypatch.setattr(uvicorn, "run", fake_uvicorn_run)
    # The repo's ``config.yaml`` is gitignored and absent, so this exercises the
    # example-config fallback — the normal path for a fresh checkout.
    monkeypatch.chdir(EXAMPLE_CONFIG_PATH.parent)

    run_cli([])

    captured = capsys.readouterr()
    assert EXPECTED_BASE_URL in captured.out
    assert f"Set Base URL to {EXPECTED_BASE_URL}" in captured.out

    assert len(run_calls) == 1
    assert run_calls[0]["host"] == EXAMPLE_CONFIG.gateway.host
    assert run_calls[0]["port"] == EXAMPLE_CONFIG.gateway.port
    assert run_calls[0]["log_level"] == EXAMPLE_CONFIG.gateway.log_level
    assert isinstance(run_calls[0]["app"], FastAPI)


def test_python_dash_m_gateway_help_runs_as_a_child_process() -> None:
    """``python -m gateway --help`` works as a real child process.

    This is the only test that exercises the actual ``python -m gateway``
    invocation end to end. ``--help`` is handled by argparse, which raises
    ``SystemExit(0)`` *before* :func:`gateway.main.run_cli` reaches
    ``uvicorn.run``, so the child cannot bind a port — a bound-and-blocking
    server would instead hit the timeout below. The prompt-exit assertion pins
    that behaviour (a real server would never return on its own).

    ``PYTHONPATH`` is pointed at the project's ``src`` directory so the child
    resolves the package deterministically, whether or not the project has been
    ``pip install``-ed. ``sys.executable`` reuses the interpreter running the
    suite, so no environment-specific path is hard-coded.
    """
    env = {**os.environ, "PYTHONPATH": str(SRC_DIR)}

    started = time.perf_counter()
    completed = subprocess.run(
        [sys.executable, "-m", "gateway", "--help"],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    elapsed = time.perf_counter() - started

    assert completed.returncode == 0
    assert "usage: gateway" in completed.stdout
    assert "--config" in completed.stdout
    assert "config.example.yaml" in completed.stdout
    assert completed.stderr == ""
    # Exited promptly instead of binding a port and blocking.
    assert elapsed < 15.0
