"""Process-local, reported-usage-only dashboard telemetry; never a relay dependency."""
from __future__ import annotations

import json
import sys
from collections import deque
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from typing import Any

import anyio

from gateway.config import RouteConfig, RouteTargetConfig, RoutingProfile
from gateway.engine.registry.models import ChannelNode

PRICING_NOTE = "estimated USD; registry prices assumed USD, no FX conversion"
_MAX_FLOAT = Decimal(str(sys.float_info.max))


def _count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


@dataclass(frozen=True)
class Usage:
    total: int | None
    prompt: int | None
    cached: int


def reported_usage(body: object) -> Usage | None:
    """Accept strict counts only; missing/invalid total is not an estimated total."""
    if not isinstance(body, dict) or not isinstance(body.get("usage"), dict):
        return None
    usage = body["usage"]
    prompt = _count(usage.get("prompt_tokens"))
    completion = _count(usage.get("completion_tokens"))
    total = _count(usage.get("total_tokens"))
    if total is None and prompt is not None and completion is not None:
        total = prompt + completion
    if prompt is not None and total is not None:
        prompt = min(prompt, total)
    details = usage.get("prompt_tokens_details")
    cached = _count(details.get("cached_tokens")) if isinstance(details, dict) else None
    # Without a prompt denominator, cached usage cannot safely contribute to
    # either prompt-cache hit rate or attributable savings.
    cached = min(cached or 0, prompt) if prompt is not None else 0
    if total is None and prompt is None:
        return None
    return Usage(total, prompt, cached)


def savings_channel(
    route: RouteConfig, alias: str, cards: Sequence[ChannelNode], target: RouteTargetConfig | None
) -> ChannelNode | None:
    """Only price a uniquely identified trusted binding, never publisher/provider guesses.

    Duplicate bindings are conservatively unpriced: the matcher may apply a
    different request eligibility filter or a custom estimator to those cards.
    """
    if route.strategy != "matchmaker" or target is None:
        return None
    candidates = []
    for card in cards:
        binding = route.channel_targets.get(card.channel_id)
        if (card.model_name == alias and binding is not None
                and (binding.provider, binding.model) == (target.provider, target.model)):
            candidates.append(card)
    return candidates[0] if len(candidates) == 1 else None


class DashboardTelemetry:
    """Single event-loop owned state. No locks, I/O, shared globals or background tasks."""

    def __init__(self, profile: RoutingProfile = "balanced") -> None:
        self.active_profile = profile
        self.profile_override: RoutingProfile | None = None
        self.total_requests = 0
        self.total_tokens = 0
        self.prompt_tokens = 0
        self.cached_tokens = 0
        self.usage_reports = 0
        self.unpriced_usage_reports = 0
        self._savings = Decimal(0)
        self._incidents: deque[dict[str, Any]] = deque(maxlen=100)

    def switch_profile(self, profile: RoutingProfile) -> None:
        self.active_profile = profile
        self.profile_override = profile

    def account(self, usage: Usage, card: ChannelNode | None) -> None:
        self.total_tokens += usage.total or 0
        self.prompt_tokens += usage.prompt or 0
        self.cached_tokens += usage.cached
        self.usage_reports += 1
        if card is None:
            self.unpriced_usage_reports += 1
            return
        pricing = card.pricing
        with localcontext() as context:
            context.prec = 50
            discount = max(Decimal(str(pricing.input_price_per_1m))
                           - Decimal(str(pricing.cache_read_price_per_1m)), Decimal(0))
            estimate = Decimal(usage.cached) * discount * Decimal(str(pricing.multiplier)) / 1_000_000
            self._savings = min(self._savings + estimate, _MAX_FLOAT)

    def incident(self, source: RouteTargetConfig, destination: RouteTargetConfig,
                 reason: str, duration_ms: float) -> None:
        self._incidents.appendleft({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "from_provider": source.provider, "from_model": source.model,
            "to_provider": destination.provider, "to_model": destination.model,
            "reason": reason, "duration_ms": max(0.0, min(duration_ms, sys.float_info.max)),
        })

    def snapshot(self) -> dict[str, Any]:
        return {
            "total_requests": self.total_requests, "total_tokens": self.total_tokens,
            "prompt_tokens": self.prompt_tokens, "cached_tokens": self.cached_tokens,
            "cache_hit_rate": self.cached_tokens / self.prompt_tokens if self.prompt_tokens else None,
            "cache_savings_usd": float(self._savings), "active_profile": self.active_profile,
            "global_profile_override": self.profile_override is not None,
            "recent_failovers": list(self._incidents), "usage_reports": self.usage_reports,
            "unpriced_usage_reports": self.unpriced_usage_reports,
            "pricing_note": PRICING_NOTE,
            "scope": "Process-local since lifespan start; successful upstream reported usage only; static, missing and ambiguous channel prices excluded from savings.",
        }


class DispatchObservation:
    """One request's actual serving target and transition sink.

    The router guards even overridden hooks; telemetry must never break routing.
    """

    def __init__(self, sink: DashboardTelemetry) -> None:
        self.sink = sink
        self.serving_target: RouteTargetConfig | None = None

    def served(self, target: RouteTargetConfig) -> None:
        self.serving_target = target

    def transition(self, source: RouteTargetConfig, destination: RouteTargetConfig,
                   reason: str, duration_ms: float) -> None:
        self.sink.incident(source, destination, reason, duration_ms)


def account_body(sink: DashboardTelemetry, body: object, card: ChannelNode | None) -> None:
    """Best effort: even malformed upstream structures cannot affect a valid relay."""
    try:
        usage = reported_usage(body)
        if usage is not None:
            sink.account(usage, card)
    except Exception:
        pass


class _SSEUsageObserver:
    """Byte-incremental SSE parser, bounded to64KiB/event including ignored fields."""

    def __init__(self) -> None:
        self.latest: Usage | None = None
        self._line = bytearray()
        self._data: list[bytes] = []
        self._size = 0
        self._discard = False
        self._skip_lf = False
        self._line_nonempty = False
        self._done = False

    def feed(self, chunk: bytes) -> None:
        for value in chunk:
            if self._done:
                return
            if self._skip_lf and value == 10:
                self._skip_lf = False
                continue
            self._skip_lf = value == 13
            if value in (10, 13):
                self._newline()
                continue
            self._line_nonempty = True
            self._size += 1
            if self._size > 65536:
                self._discard = True
                self._line.clear()
                self._data.clear()
            if not self._discard:
                self._line.append(value)

    def _newline(self) -> None:
        if not self._line_nonempty:
            self._event()
            self._size = 0
            self._discard = False
        elif not self._discard:
            line = bytes(self._line)
            if line.startswith(b"data:"):
                self._data.append(line[6:] if line.startswith(b"data: ") else line[5:])
            self._size += 1
        self._line.clear()
        self._line_nonempty = False

    def _event(self) -> None:
        if self._discard or not self._data:
            self._data.clear()
            return
        raw = b"\n".join(self._data)
        self._data.clear()
        if raw.strip() == b"[DONE]":
            self._done = True
            return
        try:
            usage = reported_usage(json.loads(raw))
            if usage is not None:
                self.latest = usage
        except (ValueError, UnicodeError, RecursionError):
            pass

    def finish(self) -> None:
        if not self._done:
            if self._line_nonempty:
                self._newline()
            self._event()


class _ObservedStream(AsyncIterator[bytes]):
    """Own the original iterator even before the first downstream pull."""

    def __init__(self, iterator: AsyncIterator[bytes], sink: DashboardTelemetry,
                 card: ChannelNode | None) -> None:
        self._iterator = iterator
        self._sink = sink
        self._card = card
        self._observer = _SSEUsageObserver()
        self._closed = False

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        if self._closed:
            raise StopAsyncIteration
        try:
            chunk = await self._iterator.__anext__()
        except BaseException:
            await self.aclose()
            raise
        try:
            self._observer.feed(chunk)
        except Exception:
            pass
        return chunk

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._observer.finish()
            if self._observer.latest is not None:
                self._sink.account(self._observer.latest, self._card)
        except Exception:
            pass
        finally:
            close = getattr(self._iterator, "aclose", None)
            if close is not None:
                with anyio.CancelScope(shield=True):
                    await close()


def observe_stream(iterator: AsyncIterator[bytes], sink: DashboardTelemetry,
                   card: ChannelNode | None) -> AsyncIterator[bytes]:
    """Observe only pulled bytes; close the original and count final usage once."""
    return _ObservedStream(iterator, sink, card)
