"""Core transparent reverse-proxy engine for the ShareLLM Smart Gateway.

The engine forwards one OpenAI-style chat-completions payload to a single
upstream :class:`~gateway.config.RouteTargetConfig` and relays the result back
to the caller in one of two shapes:

* ``stream=False`` — the upstream body is awaited in full and returned as a
  ``(status_code, json_body, headers)`` triple.
* ``stream=True`` — the upstream stream is inspected before the caller's stream
  commits (first-chunk / TTFT inspection) and returned as a
  ``(status_code, error_body, headers, chunk_iterator | None)`` quadruple. On a
  success status ``chunk_iterator`` relays the buffered first chunk followed by
  the remaining upstream bytes, without ever buffering the whole body in memory;
  on an error status ``error_body`` carries the upstream error payload and
  ``chunk_iterator`` is ``None`` so a failover router can try the next target.

Both flows go through the same :func:`forward_request` entry point; the return
type is selected with :func:`typing.overload` so static type checkers see the
precise shape for each ``stream`` literal.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Literal, Mapping, TypeAlias, overload

import httpx

from gateway.config import ProviderConfig, RouteTargetConfig

__all__ = [
    "BufferedResult",
    "JSONValue",
    "StreamingResult",
    "forward_request",
]

#: Any value a JSON document may hold. Used instead of a bare ``Any`` so the
#: buffered response body has a real, self-describing type.
JSONValue: TypeAlias = dict[str, Any] | list[Any] | str | int | float | bool | None

#: Return shape of a buffered (``stream=False``) forward: ``(status, body, headers)``.
BufferedResult: TypeAlias = tuple[int, JSONValue, dict[str, str]]

#: Return shape of a streaming (``stream=True``) forward:
#: ``(status, error_body, headers, chunk_iterator)``. ``chunk_iterator`` is
#: ``None`` exactly when ``status >= 400`` (the stream is inspected, released
#: and reported as an error so a failover router can react before any byte is
#: sent to the caller).
StreamingResult: TypeAlias = tuple[
    int, JSONValue, dict[str, str], AsyncIterator[bytes] | None
]

#: Path appended to a provider's ``base_url`` to reach its chat endpoint.
_UPSTREAM_CHAT_PATH = "/chat/completions"

#: Hop-by-hop headers (RFC 9110 §7.6.1 / RFC 7230 §6.1) that a proxy must
#: consume itself and never forward: they describe a single transport hop, so
#: relaying them would corrupt the upstream connection's framing.
_HOP_BY_HOP_HEADERS: frozenset[str] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

#: End-to-end request headers that a proxy must *not* copy verbatim: ``host``
#: and ``content-length`` describe the original hop and are recomputed by the
#: forwarding client for the upstream authority / re-serialised body, while
#: ``authorization`` is replaced with the provider credential.
_REQUEST_HEADERS_REBUILT_BY_CLIENT: frozenset[str] = frozenset(
    {"host", "content-length", "authorization"}
)

#: Headers dropped from an inbound request before it is forwarded upstream.
_STRIPPED_REQUEST_HEADERS: frozenset[str] = (
    _HOP_BY_HOP_HEADERS | _REQUEST_HEADERS_REBUILT_BY_CLIENT
)

#: Headers dropped from an upstream response before it is relayed to the
#: caller. ``content-length``/``content-encoding``/``content-type`` are dropped
#: because the gateway re-frames the body (JSON re-serialisation on the buffered
#: path, ``text/event-stream`` on the streaming path), so the upstream framing
#: no longer applies.
_STRIPPED_RESPONSE_HEADERS: frozenset[str] = _HOP_BY_HOP_HEADERS | frozenset(
    {"content-length", "content-encoding", "content-type"}
)

#: Upper bound on how much of a non-JSON upstream error body is echoed back.
_MAX_ERROR_BODY_CHARS = 500


def _chat_completions_url(base_url: str) -> str:
    """Join a provider ``base_url`` with the upstream chat-completions path."""
    return base_url.rstrip("/") + _UPSTREAM_CHAT_PATH


def _connection_nominated_headers(headers: Mapping[str, str]) -> set[str]:
    """Return the lower-cased header names nominated by inbound ``Connection``.

    RFC 9110 §7.6.1 requires a proxy to treat every field named in the
    ``Connection`` field as hop-by-hop for this message — e.g. an inbound
    ``Connection: X-Internal`` means ``X-Internal`` must not be forwarded. The
    ``Connection`` field itself is comma-separated, so every element is
    collected (and ``close``/``keep-alive`` are harmless no-ops here because no
    such header exists to drop).
    """
    nominated: set[str] = set()
    for name, value in headers.items():
        if name.lower() != "connection":
            continue
        nominated.update(
            element.strip().lower() for element in value.split(",") if element.strip()
        )
    return nominated


def _build_upstream_headers(headers: Mapping[str, str], api_key: str) -> dict[str, str]:
    """Return the header set to send upstream for one forwarded request.

    Strips hop-by-hop headers plus the headers the forwarding client rebuilds
    (``host``, ``content-length``) and any inbound ``authorization``, then sets
    the provider credential. Matching is case-insensitive because HTTP header
    names are case-insensitive. Any header *nominated* by the inbound
    ``Connection`` field is dropped as well (RFC 9110 §7.6.1).

    Args:
        headers: The inbound request headers, as received from the caller.
        api_key: The upstream provider credential.

    Returns:
        A new, sanitised header mapping.
    """
    dropped = _STRIPPED_REQUEST_HEADERS | _connection_nominated_headers(headers)
    sanitized = {
        name: value for name, value in headers.items() if name.lower() not in dropped
    }
    sanitized["Authorization"] = f"Bearer {api_key}"
    return sanitized


def _forwardable_response_headers(response: httpx.Response) -> dict[str, str]:
    """Return the upstream response headers that are safe to relay back."""
    return {
        name: value
        for name, value in response.headers.items()
        if name.lower() not in _STRIPPED_RESPONSE_HEADERS
    }


def _non_json_body_error(response: httpx.Response) -> dict[str, JSONValue]:
    """Wrap a non-JSON upstream body in an OpenAI-style error envelope.

    Upstreams sometimes answer with an HTML/plain-text error page. Returning it
    verbatim would break the gateway's JSON contract, so it is embedded in a
    structured error object instead of raising.
    """
    body = response.text
    if len(body) > _MAX_ERROR_BODY_CHARS:
        body = body[:_MAX_ERROR_BODY_CHARS] + "…"
    return {
        "error": {
            "message": (
                f"Upstream returned a non-JSON response body "
                f"(HTTP {response.status_code}): {body}"
            ),
            "type": "upstream_error",
            "param": None,
            "code": None,
        }
    }


async def _forward_buffered(
    client: httpx.AsyncClient,
    url: str,
    request_data: dict[str, Any],
    headers: dict[str, str],
    timeout: httpx.Timeout,
) -> BufferedResult:
    """Send a non-streaming request and decode the upstream response body."""
    response = await client.post(url, json=request_data, headers=headers, timeout=timeout)
    try:
        payload: JSONValue = response.json()
    except ValueError:
        payload = _non_json_body_error(response)
    return response.status_code, payload, _forwardable_response_headers(response)


async def _read_error_body(response: httpx.Response) -> JSONValue:
    """Read an upstream error body and release the response in every case.

    The body is read with ``aread()`` (a streamed response has no decoded
    content until it is read) and the upstream response is closed in a
    ``finally`` so a failed attempt never leaves an open connection behind. A
    non-JSON body is funnelled through :func:`_non_json_body_error` rather than
    being allowed to raise.
    """
    try:
        await response.aread()
        try:
            # Bind to a ``JSONValue``-annotated name so the declared return type
            # holds without leaking ``httpx.Response.json()``'s ``Any`` (the
            # same narrowing ``_forward_buffered`` already applies).
            payload: JSONValue = response.json()
        except ValueError:
            return _non_json_body_error(response)
        return payload
    finally:
        await response.aclose()


async def _chained_stream(
    response: httpx.Response,
    first_chunk: bytes | None,
    upstream: AsyncIterator[bytes],
) -> AsyncIterator[bytes]:
    """Relay the buffered first chunk, then the rest, releasing on every exit.

    The upstream response is owned by this generator and released in a
    ``finally`` rather than through an async-context-manager ``__aexit__``. That
    keeps the Task 1.2 zero-leak guarantee intact on **every** exit path —
    full drain, early ``aclose()``, upstream failure mid-stream, and the
    client-disconnect path where the response layer cancels this generator while
    it is parked at a ``yield`` (including at the *buffered first chunk's*
    ``yield``, before the remaining upstream bytes have been touched).
    """
    try:
        if first_chunk is not None:
            yield first_chunk
        async for chunk in upstream:
            yield chunk
    finally:
        await response.aclose()


async def _forward_streaming(
    client: httpx.AsyncClient,
    request: httpx.Request,
) -> StreamingResult:
    """Open an upstream stream, inspect it, and return a relayable result.

    The response is obtained with an explicit ``client.send(..., stream=True)``
    so it can be released with a direct ``await response.aclose()`` (the nested
    ``@asynccontextmanager`` that ``client.stream(...)`` returns re-enters
    ``contextlib._AsyncGeneratorContextManager.__aexit__`` on the
    async-generator ``athrow`` path, which can raise
    ``RuntimeError("generator didn't stop after athrow()")`` when a close races
    the finalizer).

    First-chunk inspection (TTFT):

    * ``status >= 400`` — the error body is read and the upstream response is
      released immediately, and ``(status, error_body, headers, None)`` is
      returned so a failover router can pick the next target *before* any byte
      reaches the caller.
    * success — the first data chunk is pulled eagerly to prove the stream is
      viable, and ``(status, None, headers, chained_generator)`` is returned.
      The generator yields the buffered first chunk followed by the remaining
      ``response.aiter_bytes()``; no further chunk is read ahead, so nothing is
      buffered beyond that single first chunk.
    """
    response = await client.send(request, stream=True)
    status_code = response.status_code

    if status_code >= 400:
        headers = _forwardable_response_headers(response)
        error_body = await _read_error_body(response)
        return status_code, error_body, headers, None

    headers = _forwardable_response_headers(response)
    upstream = response.aiter_bytes()
    try:
        first_chunk: bytes | None = await upstream.__anext__()
    except StopAsyncIteration:
        first_chunk = None
    except BaseException:
        # A failure while proving stream viability (e.g. a read timeout before
        # the first chunk) must not leak the half-open upstream response.
        await response.aclose()
        raise
    return status_code, None, headers, _chained_stream(response, first_chunk, upstream)


@overload
async def forward_request(
    client: httpx.AsyncClient,
    target: RouteTargetConfig,
    provider: ProviderConfig,
    request_data: dict[str, Any],
    stream: Literal[False],
    headers: dict[str, str],
) -> BufferedResult: ...


@overload
async def forward_request(
    client: httpx.AsyncClient,
    target: RouteTargetConfig,
    provider: ProviderConfig,
    request_data: dict[str, Any],
    stream: Literal[True],
    headers: dict[str, str],
) -> StreamingResult: ...


async def forward_request(
    client: httpx.AsyncClient,
    target: RouteTargetConfig,
    provider: ProviderConfig,
    request_data: dict[str, Any],
    stream: bool,
    headers: dict[str, str],
) -> BufferedResult | StreamingResult:
    """Forward one chat-completions payload to a single upstream target.

    Args:
        client: The shared, pooled :class:`httpx.AsyncClient` to send with.
        target: The selected route target (supplies the upstream model and the
            per-request timeout).
        provider: The provider that serves ``target`` (supplies the base URL
            and the credential).
        request_data: The OpenAI-style request payload to forward.
        stream: ``False`` for a buffered JSON response, ``True`` for a raw byte
            stream relayed from the upstream.
        headers: The inbound request headers, before sanitation.

    Returns:
        With ``stream=False``: ``(status_code, json_body, response_headers)``.
        With ``stream=True``:
        ``(status_code, error_body, response_headers, chunk_iterator)`` where
        ``chunk_iterator`` is ``None`` for a ``status >= 400`` attempt and a byte
        iterator for a success attempt.
    """
    url = _chat_completions_url(provider.base_url)
    upstream_headers = _build_upstream_headers(headers, provider.api_key)
    timeout = httpx.Timeout(target.timeout_seconds)

    if stream:
        request = client.build_request(
            "POST", url, json=request_data, headers=upstream_headers, timeout=timeout
        )
        return await _forward_streaming(client, request)
    return await _forward_buffered(
        client, url, request_data, upstream_headers, timeout
    )
