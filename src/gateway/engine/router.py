"""Resilient failover engine for the ShareLLM Smart Gateway.

:func:`dispatch` walks a route's ordered targets and forwards one
chat-completions payload to the first target that answers successfully. A
target is treated as *failed* — and the next target is tried — when the
upstream:

* answers with a retryable status (``429``/``500``/``502``/``503``/``504``), or
* raises a retryable transport error (:class:`httpx.TimeoutException` or
  :class:`httpx.NetworkError`).

Every other outcome (any non-retryable status such as ``400``/``401``/``404``,
or a success) is returned to the caller **immediately**, with the upstream body
and headers, without burning the remaining targets.

Failures also feed a per-application :class:`CooldownRegistry` (a soft circuit
breaker): a target that just failed is skipped by subsequent requests for
:data:`DEFAULT_COOLDOWN_SECONDS`, unless *every* target in the route is cooling
down, in which case the full ordered list is used again.

Each attempt forwards a **per-target copy** of the caller's payload with
``model`` rewritten to the target's upstream identifier, so a client that asks
for an alias (say ``gpt-6-astra``) reaches a backup declaring a different model
id (``gpt-6-astra`` or ``deepseek-chat``) under *its* own expected name. The
caller's dict is never mutated — the rewrite lives only on the copy handed to
that one attempt.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Literal, TypeAlias, overload

import httpx

from gateway.config import AppConfig, RouteConfig, RouteTargetConfig
from gateway.engine.proxy import (
    BufferedResult,
    JSONValue,
    StreamingResult,
    forward_request,
)

__all__ = [
    "BufferedDispatch",
    "CooldownRegistry",
    "DEFAULT_COOLDOWN_SECONDS",
    "RETRYABLE_STATUS_CODES",
    "StreamingDispatch",
    "dispatch",
]

logger = logging.getLogger(__name__)

#: Upstream HTTP statuses that trigger a failover to the next target.
RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})

#: How long (in seconds) a failed target is skipped by later requests.
DEFAULT_COOLDOWN_SECONDS: float = 30.0

#: Status reported when an attempt fails without producing an HTTP response.
_TIMEOUT_STATUS_CODE = 504
_NETWORK_ERROR_STATUS_CODE = 502
_DEFAULT_FAILURE_STATUS_CODE = 502

#: Stable identity of an upstream target for cooldown bookkeeping. The same
#: provider + model pair is the same upstream endpoint, wherever it is routed.
TargetKey: TypeAlias = tuple[str, str]

#: A buffered dispatch result paired with the *name* of the provider that
#: produced it (``None`` when every target failed and the gateway's own
#: ``gateway_failover_exhausted`` envelope is returned). The provider name lets
#: the HTTP layer attribute per-response telemetry — e.g. the ShareLLM cache-hit
#: log line — to the upstream that actually served the request, which after a
#: failover is not necessarily the route's first target.
BufferedDispatch: TypeAlias = tuple[BufferedResult, str | None]

#: A streaming dispatch result paired with the serving provider's name.
StreamingDispatch: TypeAlias = tuple[StreamingResult, str | None]


def _target_key(target: RouteTargetConfig) -> TargetKey:
    """Return the cooldown key for ``target``."""
    return (target.provider, target.model)


class CooldownRegistry:
    """In-memory soft circuit breaker for upstream targets.

    The registry is deliberately an *instance* (not module state) so it can be
    created per application in the ASGI lifespan and reset between tests. A
    module-global registry would leak cooldown state across the test suite and
    make it order-dependent.

    The clock is injectable so a test can reason about expiry deterministically
    without sleeping.
    """

    def __init__(
        self,
        ttl_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._expiries: dict[TargetKey, float] = {}

    @property
    def ttl_seconds(self) -> float:
        """The cooldown lifetime, in seconds."""
        return self._ttl_seconds

    def record_failure(self, target: RouteTargetConfig) -> None:
        """Start (or restart) ``target``'s cooldown window."""
        self._expiries[_target_key(target)] = self._clock() + self._ttl_seconds

    def is_cooling(self, target: RouteTargetConfig) -> bool:
        """Return whether ``target`` is currently inside its cooldown window."""
        key = _target_key(target)
        expiry = self._expiries.get(key)
        if expiry is None:
            return False
        if self._clock() >= expiry:
            del self._expiries[key]
            return False
        return True

    def reset(self) -> None:
        """Forget every recorded cooldown (a clean registry)."""
        self._expiries.clear()


def _select_targets(
    route: RouteConfig, registry: CooldownRegistry
) -> list[RouteTargetConfig]:
    """Return the targets to try, honouring the soft cooldown.

    Cooling targets are skipped while at least one target is available; if every
    target is cooling down, the full ordered list is returned so the request
    still has somewhere to go.
    """
    targets = list(route.targets)
    available = [target for target in targets if not registry.is_cooling(target)]
    return available if available else targets


def _failover_exhausted_envelope() -> dict[str, JSONValue]:
    """Build the OpenAI-style envelope returned when every target has failed."""
    envelope: dict[str, JSONValue] = {
        "error": {
            "message": "All upstream targets failed",
            "type": "gateway_failover_exhausted",
        }
    }
    return envelope


def _log_failover(
    target: RouteTargetConfig,
    reason: str,
    remaining: int,
) -> None:
    """Log one failed attempt at WARNING level.

    The wording distinguishes a real failover (``remaining > 0``, so the next
    target will actually be tried) from the final attempt (``remaining == 0``,
    where the route is about to be exhausted and there is nothing left to fail
    over to). A gateway's logs must never claim a failover that does not happen,
    so the "failing over" phrasing is used only when a further target remains.
    """
    if remaining > 0:
        logger.warning(
            "Upstream target '%s' (model '%s') failed (%s); failing over to the "
            "next target. %d target(s) remaining.",
            target.provider,
            target.model,
            reason,
            remaining,
        )
        return
    logger.warning(
        "Upstream target '%s' (model '%s') failed (%s); no target remains "
        "(last target in the route).",
        target.provider,
        target.model,
        reason,
    )


@overload
async def dispatch(
    client: httpx.AsyncClient,
    route: RouteConfig,
    config: AppConfig,
    request_data: dict[str, Any],
    stream: Literal[False],
    headers: dict[str, str],
    registry: CooldownRegistry,
) -> BufferedDispatch: ...


@overload
async def dispatch(
    client: httpx.AsyncClient,
    route: RouteConfig,
    config: AppConfig,
    request_data: dict[str, Any],
    stream: Literal[True],
    headers: dict[str, str],
    registry: CooldownRegistry,
) -> StreamingDispatch: ...


async def dispatch(
    client: httpx.AsyncClient,
    route: RouteConfig,
    config: AppConfig,
    request_data: dict[str, Any],
    stream: bool,
    headers: dict[str, str],
    registry: CooldownRegistry,
) -> BufferedDispatch | StreamingDispatch:
    """Forward ``request_data`` down ``route`` with resilient failover.

    Args:
        client: The shared, pooled upstream client.
        route: The route whose ordered targets are tried.
        config: The application config (resolves each target's provider).
        request_data: The OpenAI-style request payload. It is **not** mutated:
            each attempt forwards a copy whose ``model`` is rewritten to that
            target's upstream model identifier.
        stream: ``True`` for a streaming relay, ``False`` for a buffered body.
        headers: The inbound request headers, before sanitation.
        registry: The cooldown registry used to skip recently failed targets.

    Returns:
        ``(result, provider_name)`` for the first target that does not fail,
        where ``result`` is the same shape
        :func:`~gateway.engine.proxy.forward_request` produces — a
        ``(status, body, headers)`` triple for buffered, or a
        ``(status, error_body, headers, chunk_iterator)`` quadruple for
        streaming — and ``provider_name`` is the name of the provider that
        served it. When *every* target fails, the last target's status code is
        returned with the ``gateway_failover_exhausted`` envelope (and a ``None``
        chunk iterator for the streaming shape) and a ``None`` provider name.
    """
    targets = _select_targets(route, registry)

    last_status_code = _DEFAULT_FAILURE_STATUS_CODE
    last_headers: dict[str, str] = {}

    for index, target in enumerate(targets):
        provider = config.providers[target.provider]
        remaining = len(targets) - index - 1
        # Per-target copy: rewriting ``model`` on the caller's own dict would
        # leak this attempt's upstream identifier into every later attempt (and
        # back to the caller). The copy is shallow, so nested values (messages,
        # tools, …) are shared read-only with the caller.
        target_payload = {**request_data, "model": target.model}

        try:
            if stream:
                status_code, body, response_headers, chunk_iterator = (
                    await forward_request(
                        client, target, provider, target_payload, True, headers
                    )
                )
            else:
                status_code, body, response_headers = await forward_request(
                    client, target, provider, target_payload, False, headers
                )
                chunk_iterator = None
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            last_status_code = (
                _TIMEOUT_STATUS_CODE
                if isinstance(exc, httpx.TimeoutException)
                else _NETWORK_ERROR_STATUS_CODE
            )
            last_headers = {}
            registry.record_failure(target)
            _log_failover(target, f"{type(exc).__name__}: {exc}", remaining)
            continue

        if status_code in RETRYABLE_STATUS_CODES:
            last_status_code = status_code
            last_headers = response_headers
            registry.record_failure(target)
            _log_failover(target, f"HTTP {status_code}", remaining)
            continue

        # Success, or a non-retryable status: hand the upstream result straight
        # back — the remaining targets are deliberately not consumed.
        if stream:
            return (status_code, body, response_headers, chunk_iterator), target.provider
        return (status_code, body, response_headers), target.provider

    logger.warning(
        "All %d upstream target(s) for route failed; returning HTTP %d.",
        len(targets),
        last_status_code,
    )
    envelope = _failover_exhausted_envelope()
    if stream:
        return (last_status_code, envelope, last_headers, None), None
    return (last_status_code, envelope, last_headers), None
