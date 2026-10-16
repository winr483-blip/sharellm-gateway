"""Opt-in offline marketplace fixtures, NOT verified marketplace observations.

Only the values identified as supplied below come from the user's fixture
taxonomy. Every price, publisher identity, capability and metric not explicitly
supplied is ASSUMED ILLUSTRATIVE data, not fresh measurements or genuine offers.
Prices use illustrative USD-per-million units; no FX conversion is performed.
No credentials, provider bindings, network access or application startup hooks
are included. Importing this module performs no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

from gateway.engine.registry.models import (
    ChannelAuthenticity,
    ChannelCapabilities,
    ChannelMetrics,
    ChannelNode,
    ChannelPricing,
)
from gateway.engine.registry.storage import ChannelRegistryStorage

__all__ = ["build_marketplace_cards", "seed_marketplace_data"]


@dataclass(frozen=True)
class _Fixture:
    """Immutable construction input; all defaults are illustrative assumptions."""

    market_id: str
    model: str
    variant: str
    label: str
    ttft: float = 2.4
    tps: float = 80.0
    cache: float = 0.8
    vision: bool = False
    tools: bool = True
    context: int = 128_000
    endpoints: tuple[str, ...] = ("chat/completions",)
    input_price: float = 1.0
    sla: float = 99.0
    hearts: int = 3
    anti_degradation: bool = False
    anti_injection: bool = False


def _card(fixture: _Fixture) -> ChannelNode:
    """Build fresh nested objects; never share mutable endpoint lists."""
    return ChannelNode(
        channel_id=(
            f"seed:{fixture.market_id}:{fixture.model}:{fixture.variant}"
        ),
        model_name=fixture.model,
        publisher=(
            "SEED FIXTURE / ASSUMED ILLUSTRATIVE / NOT VERIFIED | "
            f"marketplace #{fixture.market_id} | {fixture.label}"
        ),
        is_certified=False,
        pricing=ChannelPricing(
            input_price_per_1m=fixture.input_price,
            cache_read_price_per_1m=round(fixture.input_price * 0.05, 6),
            cache_write_price_per_1m=round(fixture.input_price * 1.25, 6),
            output_price_per_1m=round(fixture.input_price * 4.0, 6),
            multiplier=0.1,
        ),
        capabilities=ChannelCapabilities(
            supports_vision=fixture.vision,
            supports_tools=fixture.tools,
            context_window=fixture.context,
            endpoints=list(fixture.endpoints),
        ),
        authenticity=ChannelAuthenticity(
            fingerprint_status="untested",
            bazaarlink_similarity=None,
            anti_degradation=fixture.anti_degradation,
            anti_injection=fixture.anti_injection,
        ),
        metrics=ChannelMetrics(
            first_token_latency=fixture.ttft,
            tps=fixture.tps,
            cache_hit_rate=fixture.cache,
            sla_realtime=fixture.sla,
            # Historical windows are explicit illustrative assumptions, not
            # extrapolated evidence from the supplied realtime SLA fixture.
            sla_24h=98.5,
            sla_7d=97.5,
            community_hearts=fixture.hearts,
        ),
    )


def build_marketplace_cards() -> list[ChannelNode]:
    """Return 25 deterministic, independent strict-model fixture cards.

    Supplied fields: Sol discount TPS/TTFT/cache; Sol US Pro cache/vision/
    anti-degradation; Astra discount TPS/TTFT; Opus marketplace #1111 and
    CCMAX no-injection taxonomy with SLA 100%; Flash cache/TPS. The Opus SLA
    is represented only in sla_realtime; historical windows remain illustrative.
    US Pro uses #716 and Flash #2529 as fixture identity assumptions, not
    independently established marketplace identity. Remaining 20 cards are
    deliberately synthetic tier variations of the requested model taxonomy.
    """
    supplied = (
        _Fixture(
            "716", "gpt-6.1-sol", "discount", "gpt-特惠",
            ttft=4.98, tps=71.9, cache=0.706, input_price=0.328,
        ),
        _Fixture(
            "716", "gpt-6.1-sol", "us-pro", "sol US Pro",
            cache=0.913, vision=True, anti_degradation=True,
            context=1_024_000, endpoints=("responses", "gzip"),
            input_price=2.0,
        ),
        _Fixture(
            "716", "gpt-6-astra", "discount", "gpt-特惠",
            ttft=2.69, tps=90.1, input_price=0.28,
        ),
        _Fixture(
            "1111", "claude-opus-5.5", "ccmax", "正价CCMAX-无注入高防",
            sla=100.0, anti_injection=True, input_price=5.0,
            context=200_000, endpoints=("messages",),
        ),
        _Fixture(
            "2529", "deepseek-v4.1-flash", "flash", "Flash cache fixture",
            cache=0.954, tps=198.0, input_price=0.06,
        ),
    )
    cards = [_card(fixture) for fixture in supplied]
    models = (
        "gpt-6.1-sol", "gpt-6-astra", "claude-opus-5.5",
        "deepseek-v4.1-flash",
    )
    # All fields in these tier profiles and their numeric variations below
    # are ASSUMED ILLUSTRATIVE fixtures, not provider product assertions.
    tiers = (
        ("economy", False, False, 32_000, ("chat/completions",)),
        ("tools", False, True, 128_000, ("chat/completions", "responses")),
        ("vision", True, True, 200_000, ("responses",)),
        ("long-context", True, True, 1_024_000, ("responses", "gzip")),
        ("messages", False, True, 64_000, ("messages",)),
    )
    for model_index, model in enumerate(models):
        for tier_index, (tier, vision, tools, context, endpoints) in enumerate(tiers):
            cards.append(_card(_Fixture(
                market_id=f"illustrative-{model_index + 1}-{tier_index + 1}",
                model=model,
                variant=tier,
                label=f"Synthetic publisher {model_index + 1} / {tier}",
                ttft=round(1.2 + tier_index * 0.6 + model_index * 0.1, 2),
                tps=40.0 + model_index * 20.0 + tier_index * 8.0,
                cache=round(0.6 + tier_index * 0.06, 2),
                vision=vision,
                tools=tools,
                context=context,
                endpoints=endpoints,
                input_price=round(0.2 + model_index * 0.3 + tier_index * 0.15, 2),
                sla=96.0 + tier_index * 0.7,
                hearts=(model_index + tier_index) % 6,
            )))
    return cards


async def seed_marketplace_data(storage: ChannelRegistryStorage) -> None:
    """Upsert offline fixtures into an explicitly initialized caller-owned store.

    The caller owns initialize/close; normal storage errors and cancellation
    propagate. Writes are sequential and bounded by existing storage admission.
    No rows are deleted and unrelated IDs are untouched. Repeated successful
    calls yield identical seed cards (same seed IDs are intentionally replaced).
    This is not a whole-batch transaction: cancellation/error may leave a prefix
    written; a later explicit retry converges to the same complete fixture set.
    """
    for card in build_marketplace_cards():
        await storage.upsert_channel(card)
