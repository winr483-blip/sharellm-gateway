"""Strict, provider-neutral models for the channel registry."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ChannelAuthenticity",
    "ChannelCapabilities",
    "ChannelMetrics",
    "ChannelNode",
    "ChannelPricing",
]


class _StrictModel(BaseModel):
    """Base configuration shared by every nested registry model."""

    model_config = ConfigDict(strict=True, extra="forbid")


class ChannelPricing(_StrictModel):
    """Prices and an upstream-provided multiplier, without currency assumptions."""

    input_price_per_1m: float = Field(ge=0.0, allow_inf_nan=False)
    cache_read_price_per_1m: float = Field(ge=0.0, allow_inf_nan=False)
    cache_write_price_per_1m: float = Field(ge=0.0, allow_inf_nan=False)
    output_price_per_1m: float = Field(ge=0.0, allow_inf_nan=False)
    multiplier: float = Field(ge=0.0, allow_inf_nan=False)


class ChannelCapabilities(_StrictModel):
    """Capabilities used by registry filters."""

    supports_vision: bool
    supports_tools: bool
    context_window: int = Field(default=1_024_000, ge=1, strict=True)
    endpoints: list[str] = Field(default_factory=lambda: ["responses", "gzip"])


class ChannelAuthenticity(_StrictModel):
    """Authenticity signals supplied by the marketplace or verification path."""

    fingerprint_status: Literal["matched", "differs", "untested"]
    bazaarlink_similarity: float | None = Field(
        default=None, ge=0.0, allow_inf_nan=False
    )
    anti_degradation: bool = False
    anti_injection: bool = False


class ChannelMetrics(_StrictModel):
    """Marketplace metrics retained as supplied; no synthetic windows are inferred."""

    first_token_latency: float = Field(ge=0.0, allow_inf_nan=False)
    tps: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    cache_hit_rate: float = Field(default=0.0, ge=0.0, le=1.0, allow_inf_nan=False)
    sla_realtime: float = Field(default=100.0, ge=0.0, le=100.0, allow_inf_nan=False)
    sla_24h: float = Field(default=100.0, ge=0.0, le=100.0, allow_inf_nan=False)
    sla_7d: float = Field(default=100.0, ge=0.0, le=100.0, allow_inf_nan=False)
    community_hearts: int = Field(default=0, ge=0, strict=True)


class ChannelNode(_StrictModel):
    """A complete registry entry for one model channel."""

    channel_id: str
    model_name: str
    publisher: str
    is_certified: bool
    pricing: ChannelPricing
    capabilities: ChannelCapabilities
    authenticity: ChannelAuthenticity
    metrics: ChannelMetrics
