"""ShareLLM Smart Gateway.

Public package surface. The configuration API is re-exported here so callers
can simply write ``from gateway import load_config, AppConfig``.
"""

from __future__ import annotations

from gateway.config import (
    AppConfig,
    GatewayServerConfig,
    ProviderConfig,
    RouteConfig,
    RouteTargetConfig,
    load_config,
)

__all__ = [
    "AppConfig",
    "GatewayServerConfig",
    "ProviderConfig",
    "RouteConfig",
    "RouteTargetConfig",
    "load_config",
]

__version__ = "0.1.0"
