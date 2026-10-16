"""Typed channel registry models."""

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
    model_config = ConfigDict(extra="forbid", strict=True)


NON_NEGATIVE = Field(ge=0.0, allow_inf_nan=False)


class ChannelPricing(_StrictModel):
    input_price_per_1m: float = NON_NEGATIVE
    cache_read_price_per_1m: float = NON_NEGATIVE
    cache_write_price_per_1m: float = NON_NEGATIVE
    output_price_per_1m: float = NON_NEGATIVE
    multiplier: float = NON_NEGATIVE


class ChannelCapabilities(_StrictModel):
    supports_vision: bool
    supports_tools: bool
    context_window: int = Field(default=1_024_000, ge=1, strict=True)
    endpoints: list[str]


class ChannelAuthenticity(_StrictModel):
    fingerprint_status: Literal["matched", "differs", "untested"]
    bazaarlink_similarity: float | None = Field(
        default=None, ge=0.0, le=1.0, allow_inf_nan=False
    )
    anti_degradation: bool
    anti_injection: bool


class ChannelMetrics(_StrictModel):
    first_token_latency: float = NON_NEGATIVE
    tps: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    cache_hit_rate: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    sla_realtime: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    sla_24h: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    sla_7d: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    community_hearts: int = Field(default=0, ge=0, strict=True)


class ChannelNode(_StrictModel):
    channel_id: str
    model_name: str
    publisher: str
    is_certified: bool
    pricing: ChannelPricing
    capabilities: ChannelCapabilities
    authenticity: ChannelAuthenticity
    metrics: ChannelMetrics
