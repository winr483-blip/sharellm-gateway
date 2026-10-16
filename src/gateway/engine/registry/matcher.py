"""Request-aware channel ranking over the trusted registry/target binding.

Prices assume the multiplier applies to quoted input and cache-read prices.
Output/cache-write cost is not guessed without a request mix. The default token
estimate is a deterministic UTF-8 byte heuristic, NOT a tokenizer guarantee.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Any, TypeAlias

from gateway.config import RouteTargetConfig
from gateway.config import RoutingProfile as RoutingProfile
from gateway.engine.registry import ChannelNode, ChannelRegistryStorage

TokenEstimator: TypeAlias = Callable[[dict[str, Any]], int]


def profile_weights(profile: RoutingProfile) -> tuple[float, float, float, float]:
    """Return price, TTFT, SLA and community weights, respectively."""
    if profile == "budget":
        return (.60, .15, .15, .10)
    if profile == "balanced":
        return (.35, .35, .20, .10)
    if profile == "performance":
        return (.10, .50, .30, .10)
    raise ValueError("Unknown routing profile")


def has_image_url(value: object) -> bool:
    """Detect image keys recursively without fetching or interpreting URLs."""
    if isinstance(value, dict):
        return "image_url" in value or any(has_image_url(v) for v in value.values())
    if isinstance(value, list):
        return any(has_image_url(v) for v in value)
    return False


def estimate_input_tokens(payload: dict[str, Any]) -> int:
    """Estimate ceil(serialized prompt UTF-8 bytes / 4), including structure.

    Only messages/input/tools are counted; unrelated client metadata (including
    estimated_tokens) cannot override the estimate. Output reservation is added
    separately, including when an estimator is injected.
    """
    prompt = {key: payload[key] for key in ("messages", "input", "tools") if key in payload}
    encoded = json.dumps(prompt, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return (len(encoded) + 3) // 4


def required_tokens(payload: dict[str, Any], estimator: TokenEstimator) -> int:
    """Add the largest explicit strict, nonnegative output-token reservation."""
    estimate = estimator(payload)
    if type(estimate) is not int or estimate < 0:
        raise ValueError("Token estimator must return a nonnegative integer")
    reservation = 0
    for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        if key not in payload:
            continue
        value = payload[key]
        if type(value) is not int or value < 0:
            raise ValueError(f"{key} must be a nonnegative integer")
        reservation = max(reservation, value)
    return estimate + reservation


def effective_input_price(channel: ChannelNode) -> Decimal:
    """Cache-aware price; Decimal avoids overflow for large finite quotes."""
    pricing, metrics = channel.pricing, channel.metrics
    with localcontext() as context:
        context.prec = 50
        hit = Decimal(str(metrics.cache_hit_rate))
        return Decimal(str(pricing.multiplier)) * (
            (1 - hit) * Decimal(str(pricing.input_price_per_1m))
            + hit * Decimal(str(pricing.cache_read_price_per_1m))
        )


def normalize_lower(values: Sequence[Decimal]) -> list[float]:
    """Candidate-relative lower-is-better normalization; equal values get100."""
    if not values:
        return []
    low, high = min(values), max(values)
    if high == low:
        return [100.0] * len(values)
    with localcontext() as context:
        context.prec = 50
        return [max(0.0, min(100.0, float(100 * (high - value) / (high - low)))) for value in values]


@dataclass(frozen=True)
class ChannelScore:
    """Inspectable score components, each on the0..100 scale."""

    channel_id: str
    price: float
    latency: float
    sla: float
    hearts: float
    total: float


def score_channels(channels: Sequence[ChannelNode], profile: RoutingProfile) -> list[ChannelScore]:
    """Score exactly the supplied eligible universe, with deterministic ties."""
    weights = profile_weights(profile)
    prices = normalize_lower([effective_input_price(channel) for channel in channels])
    latencies = normalize_lower([Decimal(str(channel.metrics.first_token_latency)) for channel in channels])
    max_hearts = max((channel.metrics.community_hearts for channel in channels), default=0)
    result: list[ChannelScore] = []
    for channel, price, latency in zip(channels, prices, latencies):
        metrics = channel.metrics
        sla = (metrics.sla_realtime + metrics.sla_24h + metrics.sla_7d) / 3
        hearts = float(Decimal(metrics.community_hearts) / Decimal(max_hearts) * 100) if max_hearts else 0.0
        total = max(0.0, min(100.0, sum(w * component for w, component in zip(weights, (price, latency, sla, hearts)))))
        result.append(ChannelScore(channel.channel_id, price, latency, sla, hearts, total))
    return sorted(result, key=lambda score: (-score.total, score.channel_id))


class ChannelScorer:
    """Public typed scorer using the shared, independently testable formulas."""

    def weights(self, profile: RoutingProfile) -> tuple[float, float, float, float]:
        """Return the profile's price, latency, SLA and hearts weights."""
        return profile_weights(profile)

    def score(
        self, channels: Sequence[ChannelNode], profile: RoutingProfile
    ) -> list[ChannelScore]:
        """Rank an already-eligible candidate universe without changing it."""
        return score_channels(channels, profile)


class ChannelMatcher:
    """Bind eligible marketplace channels to explicitly trusted upstream targets."""

    def __init__(
        self,
        storage: ChannelRegistryStorage,
        channel_targets: Mapping[str, RouteTargetConfig],
        token_estimator: TokenEstimator | None = None,
    ) -> None:
        self._storage = storage
        self._scorer = ChannelScorer()
        self._targets = {key: value.model_copy(deep=True) for key, value in channel_targets.items()}
        self._estimate = token_estimator or estimate_input_tokens

    async def select_targets(
        self,
        model_name: str,
        profile: RoutingProfile,
        payload: dict[str, Any],
        limit: int = 3,
    ) -> list[RouteTargetConfig]:
        """Hard filter, deduplicate endpoints, normalize, then rank candidates.

        Duplicate bindings use the lexicographically first eligible channel ID
        before normalization, so one physical endpoint cannot skew the universe.
        """
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit must be a positive integer")
        profile_weights(profile)
        needed = required_tokens(payload, self._estimate)
        vision = has_image_url(payload)
        strict = profile == "performance"
        channels = await self._storage.get_channels_for_model(
            model_name, min_context=needed, vision_required=vision, strict_authenticity=strict
        )
        eligible: list[ChannelNode] = []
        seen: set[tuple[str, str]] = set()
        for channel in sorted(channels, key=lambda node: node.channel_id):
            target = self._targets.get(channel.channel_id)
            if target is None or channel.model_name != model_name:
                continue
            if channel.capabilities.context_window < needed:
                continue
            if vision and not channel.capabilities.supports_vision:
                continue
            if strict and channel.authenticity.fingerprint_status != "matched":
                continue
            key = (target.provider, target.model)
            if not target.provider or not target.model or key in seen:
                continue
            seen.add(key)
            eligible.append(channel)
        ranked = self._scorer.score(eligible, profile)
        return [self._targets[score.channel_id].model_copy(deep=True) for score in ranked[:limit]]
