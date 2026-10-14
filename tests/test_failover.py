"""Failover-engine tests for the ShareLLM Smart Gateway (Task 1.3).

Everything runs in-process: the gateway is driven through
:class:`httpx.ASGITransport` (no socket is bound) and the upstream providers are
intercepted by :mod:`respx` at the ``httpcore`` layer, so no real network call
is ever made. Every expected URL and model is derived from
``config.example.yaml`` rather than hard-coded.

The cooldown registry is created per application in the ASGI lifespan, so each
test that starts a fresh app also starts from a clean registry — there is no
module-global state to leak between tests.
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
from gateway.engine.router import (
    DEFAULT_COOLDOWN_SECONDS,
    RETRYABLE_STATUS_CODES,
    CooldownRegistry,
)
from gateway.main import create_app

EXAMPLE_CONFIG_PATH: Path = Path(__file__).resolve().parents[1] / "config.example.yaml"

#: The reference configuration, loaded once for the whole module.
EXAMPLE_CONFIG = load_config(EXAMPLE_CONFIG_PATH)

#: ``coding_route`` and its two ordered targets (primary, backup).
CODING_ROUTE = EXAMPLE_CONFIG.routes["coding_route"]
PRIMARY_TARGET = CODING_ROUTE.targets[0]
BACKUP_TARGET = CODING_ROUTE.targets[1]

PRIMARY_PROVIDER = EXAMPLE_CONFIG.providers[PRIMARY_TARGET.provider]
BACKUP_PROVIDER = EXAMPLE_CONFIG.providers[BACKUP_TARGET.provider]

#: The chat-completions endpoints the gateway must POST to, derived from config.
PRIMARY_URL = PRIMARY_PROVIDER.base_url.rstrip("/") + "/chat/completions"
BACKUP_URL = BACKUP_PROVIDER.base_url.rstrip("/") + "/chat/completions"

#: A model alias served by ``coding_route``.
MODEL = "gpt-6.1-sol"

GATEWAY_BASE_URL = "http://gateway.test"

#: A multi-chunk SSE body, mirroring an OpenAI streaming completion.
SSE_CHUNKS: list[bytes] = [
    b'data: {"id":"cmpl-1","choices":[{"delta":{"content":"Hel"}}]}\n\n',
    b'data: {"id":"cmpl-1","choices":[{"delta":{"content":"lo"}}]}\n\n',
    b"data: [DONE]\n\n",
]

#: A distinct success payload returned by the backup target.
BACKUP_SUCCESS_BODY: dict[str, Any] = {
    "id": "chatcmpl-backup",
    "object": "chat.completion",
    "model": "gpt-6-astra",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hello from the backup"},
            "finish_reason": "stop",
        }
    ],
}


def _inbound_payload(stream: bool) -> dict[str, Any]:
    """Build the OpenAI-style request the caller sends to the gateway."""
    return {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": stream,
    }


class RecordingByteStream(httpx.AsyncByteStream):
    """Upstream body stream that records which chunks were released and closure.

    Used to prove that an upstream error body is released after the failover
    decision, that only the first chunk is read ahead on the success path, and
    that a client disconnect releases every stream opened during the attempt
    sequence.
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
    ``app.state.http_client``, ``app.state.app_config`` and
    ``app.state.cooldown_registry`` exist before the first request.
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
# Test 1 — buffered failover on a retryable status
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_buffered_failover_on_retryable_status(gateway: GatewayHarness) -> None:
    """Target 1 returns 429 → the request fails over to target 2's 200 body."""
    with respx.mock(assert_all_called=False) as mock:
        primary = mock.post(PRIMARY_URL).mock(
            return_value=httpx.Response(
                429,
                json={"error": {"message": "primary rate limited"}},
                headers={"content-type": "application/json"},
            )
        )
        backup = mock.post(BACKUP_URL).mock(
            return_value=httpx.Response(
                200,
                json=BACKUP_SUCCESS_BODY,
                headers={"content-type": "application/json"},
            )
        )

        response = await gateway.client.post(
            "/v1/chat/completions", json=_inbound_payload(stream=False)
        )

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

    assert response.status_code == 200
    assert response.json() == BACKUP_SUCCESS_BODY


# --------------------------------------------------------------------------- #
# Test 2 — streaming failover relays the backup SSE stream byte-exact
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_streaming_failover_relays_backup_sse_byte_exact(
    gateway: GatewayHarness,
) -> None:
    """Target 1 returns 502 → target 2's ``text/event-stream`` is relayed exactly."""
    primary_stream = RecordingByteStream([b'{"error": {"message": "bad gateway"}}'])
    backup_stream = RecordingByteStream(SSE_CHUNKS)

    with respx.mock(assert_all_called=False) as mock:
        primary = mock.post(PRIMARY_URL).mock(
            return_value=httpx.Response(
                502,
                stream=primary_stream,
                headers={"content-type": "application/json"},
            )
        )
        backup = mock.post(BACKUP_URL).mock(
            return_value=httpx.Response(
                200,
                stream=backup_stream,
                headers={"content-type": "text/event-stream"},
            )
        )

        async with gateway.client.stream(
            "POST", "/v1/chat/completions", json=_inbound_payload(stream=True)
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            body = await response.aread()

        assert primary.call_count == 1
        assert backup.call_count == 1

    # Byte-exact, in-order relay of the backup stream.
    assert body == b"".join(SSE_CHUNKS)
    assert [event for event in body.split(b"\n\n") if event] == [
        chunk.removesuffix(b"\n\n") for chunk in SSE_CHUNKS
    ]

    # The failed attempt's error body was consumed and its upstream stream was
    # released before the failover, and the successful stream drained closed.
    assert primary_stream.closed is True
    assert primary_stream.released == [b'{"error": {"message": "bad gateway"}}']
    assert backup_stream.closed is True


# --------------------------------------------------------------------------- #
# Test 3 — failover on an upstream timeout
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_timeout_fails_over_to_backup(gateway: GatewayHarness) -> None:
    """Target 1 raises ``httpx.ReadTimeout`` → the request fails over to target 2."""
    with respx.mock(assert_all_called=False) as mock:
        primary = mock.post(PRIMARY_URL).mock(
            side_effect=httpx.ReadTimeout("upstream read timed out")
        )
        backup = mock.post(BACKUP_URL).mock(
            return_value=httpx.Response(200, json=BACKUP_SUCCESS_BODY)
        )

        response = await gateway.client.post(
            "/v1/chat/completions", json=_inbound_payload(stream=False)
        )

        assert primary.call_count == 1
        assert backup.call_count == 1

    assert response.status_code == 200
    assert response.json() == BACKUP_SUCCESS_BODY


@pytest.mark.asyncio
async def test_streaming_timeout_fails_over_to_backup(gateway: GatewayHarness) -> None:
    """A streaming attempt that times out before the first chunk fails over."""
    backup_stream = RecordingByteStream(SSE_CHUNKS)

    with respx.mock(assert_all_called=False) as mock:
        primary = mock.post(PRIMARY_URL).mock(
            side_effect=httpx.ReadTimeout("upstream read timed out")
        )
        backup = mock.post(BACKUP_URL).mock(
            return_value=httpx.Response(
                200,
                stream=backup_stream,
                headers={"content-type": "text/event-stream"},
            )
        )

        async with gateway.client.stream(
            "POST", "/v1/chat/completions", json=_inbound_payload(stream=True)
        ) as response:
            assert response.status_code == 200
            body = await response.aread()

        assert primary.call_count == 1
        assert backup.call_count == 1

    assert body == b"".join(SSE_CHUNKS)
    assert backup_stream.closed is True


# --------------------------------------------------------------------------- #
# Test 4 — every target fails with a retryable status
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_all_targets_retryable_returns_failover_exhausted_envelope(
    gateway: GatewayHarness,
) -> None:
    """All targets 429 → 429 with the ``gateway_failover_exhausted`` envelope."""
    with respx.mock(assert_all_called=False) as mock:
        primary = mock.post(PRIMARY_URL).mock(
            return_value=httpx.Response(
                429, json={"error": {"message": "primary rate limited"}}
            )
        )
        backup = mock.post(BACKUP_URL).mock(
            return_value=httpx.Response(
                429, json={"error": {"message": "backup rate limited"}}
            )
        )

        response = await gateway.client.post(
            "/v1/chat/completions", json=_inbound_payload(stream=False)
        )

        assert primary.call_count == 1
        assert backup.call_count == 1

    # The *final* target's status is surfaced, not a hard-coded 502.
    assert response.status_code == 429
    assert response.json() == {
        "error": {
            "message": "All upstream targets failed",
            "type": "gateway_failover_exhausted",
        }
    }


@pytest.mark.asyncio
async def test_all_targets_retryable_streaming_returns_json_envelope(
    gateway: GatewayHarness,
) -> None:
    """A streaming request whose targets all fail gets the JSON envelope, not SSE."""
    with respx.mock(assert_all_called=False) as mock:
        mock.post(PRIMARY_URL).mock(return_value=httpx.Response(503, json={}))
        mock.post(BACKUP_URL).mock(return_value=httpx.Response(504, json={}))

        response = await gateway.client.post(
            "/v1/chat/completions", json=_inbound_payload(stream=True)
        )

    assert response.status_code == 504  # the final target's status
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "error": {
            "message": "All upstream targets failed",
            "type": "gateway_failover_exhausted",
        }
    }


# --------------------------------------------------------------------------- #
# Test 5 — soft cooldown skips a recently failed target
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cooldown_skips_recently_failed_target(gateway: GatewayHarness) -> None:
    """After target 1 fails, an immediate request goes straight to target 2."""
    with respx.mock(assert_all_called=False) as mock:
        primary = mock.post(PRIMARY_URL).mock(
            return_value=httpx.Response(
                429, json={"error": {"message": "primary rate limited"}}
            )
        )
        backup = mock.post(BACKUP_URL).mock(
            return_value=httpx.Response(200, json=BACKUP_SUCCESS_BODY)
        )

        first = await gateway.client.post(
            "/v1/chat/completions", json=_inbound_payload(stream=False)
        )
        second = await gateway.client.post(
            "/v1/chat/completions", json=_inbound_payload(stream=False)
        )

        # Target 1's upstream was called exactly once across *both* requests:
        # the second request skipped it and went straight to target 2.
        assert primary.call_count == 1
        assert backup.call_count == 2

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == BACKUP_SUCCESS_BODY
    assert second.json() == BACKUP_SUCCESS_BODY

    registry: CooldownRegistry = gateway.app.state.cooldown_registry
    assert registry.is_cooling(PRIMARY_TARGET) is True
    assert registry.is_cooling(BACKUP_TARGET) is False


@pytest.mark.asyncio
async def test_all_targets_cooling_falls_back_to_full_order(
    gateway: GatewayHarness,
) -> None:
    """When every target is cooling, the full ordered list is used again."""
    registry: CooldownRegistry = gateway.app.state.cooldown_registry
    registry.record_failure(PRIMARY_TARGET)
    registry.record_failure(BACKUP_TARGET)

    with respx.mock(assert_all_called=False) as mock:
        primary = mock.post(PRIMARY_URL).mock(
            return_value=httpx.Response(200, json=BACKUP_SUCCESS_BODY)
        )
        backup = mock.post(BACKUP_URL).mock(
            return_value=httpx.Response(200, json=BACKUP_SUCCESS_BODY)
        )

        response = await gateway.client.post(
            "/v1/chat/completions", json=_inbound_payload(stream=False)
        )

        # Both targets were cooling, so the primary is tried again first.
        assert primary.call_count == 1
        assert backup.call_count == 0

    assert response.status_code == 200


def test_cooldown_registry_expires_and_resets() -> None:
    """The registry TTL and clock are injectable and expire deterministically."""
    now = [1_000.0]
    registry = CooldownRegistry(ttl_seconds=30.0, clock=lambda: now[0])

    assert registry.ttl_seconds == 30.0
    assert registry.is_cooling(PRIMARY_TARGET) is False

    registry.record_failure(PRIMARY_TARGET)
    assert registry.is_cooling(PRIMARY_TARGET) is True

    now[0] += 29.999
    assert registry.is_cooling(PRIMARY_TARGET) is True

    now[0] += 0.001  # exactly at the TTL boundary -> expired
    assert registry.is_cooling(PRIMARY_TARGET) is False

    registry.record_failure(PRIMARY_TARGET)
    assert registry.is_cooling(PRIMARY_TARGET) is True
    registry.reset()
    assert registry.is_cooling(PRIMARY_TARGET) is False


def test_cooldown_registries_are_independent_instances() -> None:
    """Cooldown state lives on the instance, never in module-global state."""
    first = CooldownRegistry()
    second = CooldownRegistry()

    first.record_failure(PRIMARY_TARGET)

    assert first.is_cooling(PRIMARY_TARGET) is True
    assert second.is_cooling(PRIMARY_TARGET) is False


def test_retryable_status_codes_match_the_spec() -> None:
    """Only 429 and 5xx are retryable; the default cooldown TTL is ~30 s."""
    assert RETRYABLE_STATUS_CODES == frozenset({429, 500, 502, 503, 504})
    assert DEFAULT_COOLDOWN_SECONDS == 30.0


# --------------------------------------------------------------------------- #
# Non-retryable statuses must NOT trigger failover (correct proxy behaviour)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_non_retryable_status_is_relayed_without_failover(
    gateway: GatewayHarness,
) -> None:
    """A 400 is returned immediately with the upstream body; no target is burned."""
    upstream_error = {"error": {"message": "bad request", "type": "invalid_request_error"}}

    with respx.mock(assert_all_called=False) as mock:
        primary = mock.post(PRIMARY_URL).mock(
            return_value=httpx.Response(
                400,
                json=upstream_error,
                headers={"content-type": "application/json"},
            )
        )
        backup = mock.post(BACKUP_URL).mock(
            return_value=httpx.Response(200, json=BACKUP_SUCCESS_BODY)
        )

        response = await gateway.client.post(
            "/v1/chat/completions", json=_inbound_payload(stream=False)
        )

        assert primary.call_count == 1
        # The backup target was never consulted.
        assert backup.call_count == 0

    assert response.status_code == 400
    assert response.json() == upstream_error


# --------------------------------------------------------------------------- #
# Test 6 — client disconnect during failover releases every upstream stream
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_client_disconnect_during_failover_releases_every_stream(
    gateway: GatewayHarness,
) -> None:
    """A disconnect while relaying the backup releases *every* opened stream.

    The attempt sequence is: target 1 answers 429 (its error body is consumed and
    released inside the failover engine) → target 2 opens a 200 SSE stream whose
    first chunk is buffered by the TTFT inspection → the caller disconnects while
    the response layer is parked on that buffered chunk. The assertion is made
    synchronously, before any event-loop turn could run a GC finalizer, so it
    proves release happened *at disconnect time*.
    """
    runs = 10
    request_body = json.dumps(_inbound_payload(stream=True)).encode()

    registry: CooldownRegistry = gateway.app.state.cooldown_registry

    for _ in range(runs):
        # Each run must exercise the *full* attempt sequence (target 1 fails,
        # target 2 streams). The previous run put target 1 on cooldown, so the
        # registry is reset to a clean state — this also exercises the reset API
        # the task requires.
        registry.reset()

        primary_stream = RecordingByteStream([b'{"error": {"message": "rate limited"}}'])
        backup_stream = RecordingByteStream(SSE_CHUNKS)

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

        # Synchronous assertions: no await, no gc.collect().
        assert primary_stream.closed is True, "failed attempt's stream leaked"
        assert backup_stream.closed is True, "backup stream leaked past disconnect"
        # Only the buffered first chunk was read ahead — zero-copy still holds.
        assert backup_stream.released == [SSE_CHUNKS[0]]
