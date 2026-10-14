"""Tests for the gateway HTTP surface and the transparent reverse-proxy engine.

The suite is fully in-process: the gateway is exercised through
:class:`httpx.ASGITransport` (no socket is ever bound) and the upstream provider
through :mod:`respx` (which intercepts at the ``httpcore`` layer, so it never
touches the ``ASGITransport`` used for the gateway itself).

All expected values are derived from ``config.example.yaml`` rather than
hard-coded, so the tests track the reference configuration.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import pytest
import pytest_asyncio
import respx
from fastapi import FastAPI

from gateway.config import load_config
from gateway.engine.proxy import (
    _build_upstream_headers,
    _forwardable_response_headers,
    forward_request,
)
from gateway.main import create_app

EXAMPLE_CONFIG_PATH: Path = Path(__file__).resolve().parents[1] / "config.example.yaml"

#: The reference configuration, loaded once for the whole module.
EXAMPLE_CONFIG = load_config(EXAMPLE_CONFIG_PATH)

#: The primary target of ``coding_route`` and the provider that serves it.
PRIMARY_TARGET = EXAMPLE_CONFIG.routes["coding_route"].targets[0]
PRIMARY_PROVIDER = EXAMPLE_CONFIG.providers[PRIMARY_TARGET.provider]

#: Base URL the gateway must POST to, derived from the provider config.
UPSTREAM_URL = PRIMARY_PROVIDER.base_url.rstrip("/") + "/chat/completions"
UPSTREAM_HOST = httpx.URL(UPSTREAM_URL).host

#: Host the *inbound* request spoofs; it must never reach the upstream.
SPOOFED_INBOUND_HOST = "spoofed-gateway.example"
SPOOFED_INBOUND_CONTENT_LENGTH = "9999"

GATEWAY_BASE_URL = "http://gateway.test"

#: A multi-chunk SSE body, mirroring an OpenAI streaming completion.
SSE_CHUNKS: list[bytes] = [
    b'data: {"id":"cmpl-1","choices":[{"delta":{"content":"Hel"}}]}\n\n',
    b'data: {"id":"cmpl-1","choices":[{"delta":{"content":"lo"}}]}\n\n',
    b"data: [DONE]\n\n",
]


class RecordingByteStream(httpx.AsyncByteStream):
    """Upstream body stream that records release order and closure.

    Used both to prove chunks are relayed incrementally (rather than after the
    whole body is buffered) and to prove the upstream stream is released when a
    consumer abandons it early.
    """

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.released: list[bytes] = []
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            self.released.append(chunk)
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class GatewayHarness:
    """A lifespan-started gateway app plus a client wired to its ASGI surface."""

    app: FastAPI
    client: httpx.AsyncClient


@pytest_asyncio.fixture
async def gateway() -> AsyncIterator[GatewayHarness]:
    """Start the gateway's lifespan and yield an in-process ASGI client.

    ``httpx.ASGITransport`` does not run the ASGI lifespan, so it is driven
    explicitly with ``app.router.lifespan_context(app)``. That guarantees
    ``app.state.http_client`` and ``app.state.app_config`` exist before the
    first request, exactly as they would under a real server.
    """
    application = create_app(EXAMPLE_CONFIG_PATH)
    async with application.router.lifespan_context(application):
        assert isinstance(application.state.http_client, httpx.AsyncClient)
        assert application.state.app_config is not None
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport, base_url=GATEWAY_BASE_URL
        ) as client:
            yield GatewayHarness(app=application, client=client)


@pytest.mark.asyncio
async def test_health_endpoint(gateway: GatewayHarness) -> None:
    """`GET /health` reports the gateway is up."""
    response = await gateway.client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0"}


@pytest.mark.asyncio
async def test_models_endpoint_aggregates_and_deduplicates(
    gateway: GatewayHarness,
) -> None:
    """`GET /v1/models` lists every alias across all routes, de-duplicated."""
    expected_ids: list[str] = []
    for route in EXAMPLE_CONFIG.routes.values():
        for alias in route.models:
            if alias not in expected_ids:
                expected_ids.append(alias)

    response = await gateway.client.get("/v1/models")

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"

    entries = body["data"]
    ids = [entry["id"] for entry in entries]

    # Derived from config.example.yaml: coding_route contributes
    # gpt-6.1-sol + gpt-6-astra, chat_route re-contributes gpt-6.1-sol.
    assert expected_ids == ["gpt-6.1-sol", "gpt-6-astra"]
    assert ids == expected_ids
    assert set(ids) == {"gpt-6.1-sol", "gpt-6-astra"}
    assert len(ids) == 2  # gpt-6.1-sol appears in two routes but is listed once

    for entry in entries:
        assert entry["object"] == "model"
        assert entry["owned_by"] == "sharellm-gateway"
        assert isinstance(entry["created"], int)


@pytest.mark.asyncio
async def test_non_streaming_chat_completion_is_transparent(
    gateway: GatewayHarness,
) -> None:
    """Non-streaming proxying mirrors the upstream body and sanitises headers."""
    upstream_body: dict[str, Any] = {
        "id": "chatcmpl-upstream-1",
        "object": "chat.completion",
        "model": "gpt-6.1-sol",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hello from upstream"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
    }
    inbound_payload: dict[str, Any] = {
        "model": "gpt-6.1-sol",
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": False,
    }

    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(UPSTREAM_URL).mock(
            return_value=httpx.Response(
                200, json=upstream_body, headers={"content-type": "application/json"}
            )
        )

        response = await gateway.client.post(
            "/v1/chat/completions",
            json=inbound_payload,
            headers={
                # Inbound hop-specific headers with distinctive values, so a
                # leak is unmistakable in the captured upstream request.
                "Host": SPOOFED_INBOUND_HOST,
                "Content-Length": SPOOFED_INBOUND_CONTENT_LENGTH,
                "Connection": "close",
                "Keep-Alive": "timeout=5",
                "Transfer-Encoding": "chunked",
                "Upgrade": "h2c",
                "TE": "trailers",
                "Trailer": "expires",
                "Proxy-Authenticate": "Basic realm=upstream",
                "Proxy-Authorization": "Basic cHJveHk=",
                "Proxy-Connection": "keep-alive",
                # End-to-end headers, which must survive the hop.
                "Authorization": "Bearer inbound-caller-token",
                "X-Trace-Id": "trace-123",
            },
        )

        assert route.call_count == 1
        upstream_request = route.calls.last.request

    # The client-side response mirrors the mocked upstream response.
    assert response.status_code == 200
    assert response.json() == upstream_body

    # --- transparency: assert on the *actual* upstream request respx captured ---
    assert upstream_request.method == "POST"
    assert str(upstream_request.url) == UPSTREAM_URL
    assert json.loads(upstream_request.content) == inbound_payload

    # The provider credential replaced whatever the caller sent.
    assert upstream_request.headers["authorization"] == f"Bearer {PRIMARY_PROVIDER.api_key}"

    # End-to-end headers survive the hop.
    assert upstream_request.headers["x-trace-id"] == "trace-123"

    # Hop-by-hop headers that the forwarding client does not regenerate are
    # dropped outright.
    for dropped in (
        "keep-alive",
        "transfer-encoding",
        "upgrade",
        "te",
        "trailer",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
    ):
        assert dropped not in upstream_request.headers

    # `host`, `content-length` and `connection` are always (re)generated by the
    # forwarding client — httpx sets them per request (the default
    # `Connection: keep-alive` lives at httpx/_client.py:311) — so the
    # assertion is that the spoofed inbound values never leaked through.
    assert upstream_request.headers["host"] == UPSTREAM_HOST
    assert upstream_request.headers["host"] != SPOOFED_INBOUND_HOST
    assert upstream_request.headers["content-length"] == str(len(upstream_request.content))
    assert upstream_request.headers["content-length"] != SPOOFED_INBOUND_CONTENT_LENGTH
    assert upstream_request.headers["connection"] == "keep-alive"
    assert upstream_request.headers["connection"] != "close"


@pytest.mark.asyncio
async def test_streaming_chat_completion_forwards_sse_chunks(
    gateway: GatewayHarness,
) -> None:
    """SSE chunks are relayed incrementally, in order, and released on abort."""
    inbound_payload: dict[str, Any] = {
        "model": "gpt-6.1-sol",
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": True,
    }

    # --- (A) engine level: per-chunk relay, no buffering, in-order ---------
    engine_stream = RecordingByteStream(SSE_CHUNKS)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(UPSTREAM_URL).mock(
            return_value=httpx.Response(
                200,
                stream=engine_stream,
                headers={"content-type": "text/event-stream"},
            )
        )
        status_code, error_body, response_headers, iterator = await forward_request(
            gateway.app.state.http_client,
            PRIMARY_TARGET,
            PRIMARY_PROVIDER,
            inbound_payload,
            True,
            {},
        )
        assert status_code == 200
        assert error_body is None
        assert iterator is not None

        first_chunk = await iterator.__anext__()
        # No buffering: only the first upstream chunk has been released so far.
        assert first_chunk == SSE_CHUNKS[0]
        assert engine_stream.released == [SSE_CHUNKS[0]]

        remaining = [chunk async for chunk in iterator]

    assert [first_chunk, *remaining] == SSE_CHUNKS
    # A fully drained stream is closed by the response itself.
    assert engine_stream.closed is True

    # --- (B) end-to-end through the ASGI app -------------------------------
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(UPSTREAM_URL).mock(
            return_value=httpx.Response(
                200,
                stream=RecordingByteStream(SSE_CHUNKS),
                headers={"content-type": "text/event-stream"},
            )
        )

        async with gateway.client.stream(
            "POST", "/v1/chat/completions", json=inbound_payload
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            body = await response.aread()

        assert route.call_count == 1
        upstream_request = route.calls.last.request

    # The caller receives the upstream payload byte-for-byte, in order.
    assert body == b"".join(SSE_CHUNKS)
    assert [event for event in body.split(b"\n\n") if event] == [
        chunk.removesuffix(b"\n\n") for chunk in SSE_CHUNKS
    ]

    # The streaming flag and the provider credential reached the upstream.
    assert json.loads(upstream_request.content) == inbound_payload
    assert upstream_request.headers["authorization"] == f"Bearer {PRIMARY_PROVIDER.api_key}"

    # --- (C) context safety: abandoning the stream closes the upstream -----
    abandoned_stream = RecordingByteStream(SSE_CHUNKS)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(UPSTREAM_URL).mock(
            return_value=httpx.Response(
                200,
                stream=abandoned_stream,
                headers={"content-type": "text/event-stream"},
            )
        )
        _status_code, _error_body, _response_headers, iterator = await forward_request(
            gateway.app.state.http_client,
            PRIMARY_TARGET,
            PRIMARY_PROVIDER,
            inbound_payload,
            True,
            {},
        )
        assert iterator is not None
        assert await iterator.__anext__() == SSE_CHUNKS[0]
        assert abandoned_stream.closed is False

        await iterator.aclose()  # the consumer gives up mid-stream

    assert abandoned_stream.closed is True


@pytest.mark.asyncio
async def test_unmapped_model_returns_404(gateway: GatewayHarness) -> None:
    """An unmapped model is rejected with an OpenAI-style 404 envelope."""
    response = await gateway.client.post(
        "/v1/chat/completions",
        json={
            "model": "ghost-model",
            "messages": [{"role": "user", "content": "Hi"}],
        },
    )

    assert response.status_code == 404
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["type"] == "invalid_request_error"
    assert isinstance(body["error"]["message"], str)
    assert "ghost-model" in body["error"]["message"]


def test_request_header_sanitisation_is_case_insensitive_and_honours_connection() -> None:
    """Sanitisation matches headers case-insensitively and drops Connection-nominated ones.

    ``httpx.ASGITransport`` and Starlette both lower-case inbound headers before
    the handler runs, so the end-to-end test cannot prove the engine's
    ``.lower()`` matching. This drives the helper directly with mixed-case keys,
    and also covers RFC 9110 §7.6.1: a header *named by* ``Connection`` is
    hop-by-hop for this message and must not be forwarded either.
    """
    inbound = {
        "HOST": SPOOFED_INBOUND_HOST,
        "AUTHORIZATION": "Bearer inbound-caller-token",
        "Connection": "keep-alive, X-Internal-Trace",
        "Content-Length": SPOOFED_INBOUND_CONTENT_LENGTH,
        "Keep-Alive": "timeout=5",
        "Transfer-Encoding": "chunked",
        "Upgrade": "h2c",
        "X-Internal-Trace": "internal-only",
        "X-Trace-Id": "trace-123",
    }

    sanitized = _build_upstream_headers(inbound, PRIMARY_PROVIDER.api_key)

    present = {name.lower() for name in sanitized}
    for dropped in (
        "host",
        "connection",
        "content-length",
        "keep-alive",
        "transfer-encoding",
        "upgrade",
        # nominated by the inbound `Connection` field (RFC 9110 §7.6.1)
        "x-internal-trace",
    ):
        assert dropped not in present, f"{dropped!r} leaked upstream"

    # End-to-end headers survive untouched.
    assert sanitized["X-Trace-Id"] == "trace-123"

    # Exactly one Authorization entry, set to the provider credential: the
    # inbound value is neither kept, merged nor duplicated.
    authorization_keys = [name for name in sanitized if name.lower() == "authorization"]
    assert authorization_keys == ["Authorization"]
    assert sanitized["Authorization"] == f"Bearer {PRIMARY_PROVIDER.api_key}"
    assert "inbound-caller-token" not in " ".join(sanitized.values())


def test_forwardable_response_headers_drop_framing_headers() -> None:
    """Only end-to-end upstream response headers are relayed back to the caller."""
    upstream_response = httpx.Response(200, json={"ok": True})
    # Set after construction: httpx eagerly decodes a body whose headers claim
    # `content-encoding`, and this response is only used as a header carrier.
    upstream_response.headers.update(
        {
            "content-encoding": "gzip",
            "connection": "keep-alive",
            "transfer-encoding": "chunked",
            "x-request-id": "req-1",
            "x-ratelimit-remaining": "17",
        }
    )

    forwarded = _forwardable_response_headers(upstream_response)

    assert forwarded == {"x-request-id": "req-1", "x-ratelimit-remaining": "17"}
    for dropped in (
        "content-type",
        "content-encoding",
        "connection",
        "transfer-encoding",
        "content-length",
    ):
        assert dropped not in forwarded


@pytest.mark.asyncio
async def test_non_json_upstream_error_body_is_wrapped(gateway: GatewayHarness) -> None:
    """A non-JSON (HTML) upstream error body yields an envelope, not a crash.

    The mocked upstream status is deliberately a **non-retryable** one (400).
    With Task 1.3's failover engine, the retryable set (429/500/502/503/504)
    fails over to the next target before the body ever reaches the caller, so a
    502 here would be swallowed by failover rather than relayed. A non-retryable
    status is relayed straight back with the upstream body re-serialised as JSON,
    which is exactly the path this test pins.
    """
    with respx.mock(assert_all_called=False) as mock:
        mock.post(UPSTREAM_URL).mock(
            return_value=httpx.Response(
                400,
                content=b"<html>Bad Request</html>",
                headers={"content-type": "text/html"},
            )
        )

        response = await gateway.client.post(
            "/v1/chat/completions",
            json={"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "Hi"}]},
        )

    # The upstream status code is propagated and the body is re-serialised as JSON.
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")

    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["type"] == "upstream_error"
    assert body["error"]["param"] is None
    assert body["error"]["code"] is None
    assert "HTTP 400" in body["error"]["message"]
    assert "<html>Bad Request</html>" in body["error"]["message"]


@pytest.mark.asyncio
async def test_client_disconnect_releases_upstream_stream_at_disconnect_time(
    gateway: GatewayHarness,
) -> None:
    """A client disconnect releases the upstream stream before the app returns.

    Starlette 1.8.0 never calls ``aclose()`` on a ``StreamingResponse`` body
    iterator: on disconnect it cancels ``stream_response`` and abandons the
    generator mid-iteration. The gateway's response layer therefore owns the
    iterator's lifetime, otherwise the upstream response would only be reclaimed
    by the async-generator finalizer (GC) — and closing it that way can raise
    ``RuntimeError("generator didn't stop after athrow()")``.

    The disconnect is driven through the real ASGI app on the ``spec_version
    2.3`` path (the one uvicorn uses) while the proxy generator is parked at a
    ``yield``: the client stops reading, so ``send`` blocks and ``receive``
    reports the disconnect. The assertion is made synchronously, before any
    event-loop turn could run the GC finalizer, so it proves closure happened
    *at disconnect time*. Repeated so the invariant is checked across runs
    rather than in a single race.
    """
    runs = 20
    closed_at_disconnect = 0
    loop_errors: list[str] = []

    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()

    def _record_loop_error(
        _loop: asyncio.AbstractEventLoop, context: dict[str, Any]
    ) -> None:
        exception = context.get("exception")
        if exception is not None and not isinstance(exception, asyncio.CancelledError):
            loop_errors.append(f"{type(exception).__name__}: {exception}")

    loop.set_exception_handler(_record_loop_error)
    try:
        for _ in range(runs):
            upstream_stream = RecordingByteStream(SSE_CHUNKS)
            request_body = json.dumps({"model": "gpt-6.1-sol", "stream": True}).encode()

            with respx.mock(assert_all_called=False) as mock:
                mock.post(UPSTREAM_URL).mock(
                    return_value=httpx.Response(
                        200,
                        stream=upstream_stream,
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
                    # Report the disconnect only once the generator is parked at a yield.
                    await first_body_sent.wait()
                    return {"type": "http.disconnect"}

                async def send(message: dict[str, Any]) -> None:
                    if message["type"] == "http.response.body" and message.get("body"):
                        first_body_sent.set()
                        await stalled_client.wait()

                scope: dict[str, Any] = {
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

                async with asyncio.timeout(5):
                    await gateway.app(scope, receive, send)

            # Synchronous assertion: no await, no gc.collect() — closure must
            # already have happened, not merely been deferred to finalization.
            assert upstream_stream.closed is True, "upstream stream leaked past disconnect"
            assert upstream_stream.released == [SSE_CHUNKS[0]]
            closed_at_disconnect += 1
    finally:
        loop.set_exception_handler(previous_handler)

    assert closed_at_disconnect == runs
    assert loop_errors == []
