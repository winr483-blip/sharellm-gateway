"""Typed configuration engine for the ShareLLM Smart Gateway.

This module defines the Pydantic v2 models that describe a gateway
configuration file and the :func:`load_config` entry point that reads a YAML
document and returns a fully validated :class:`AppConfig`.

Broken provider references are rejected in two complementary layers:

* :meth:`AppConfig._validate_provider_references` — a ``model_validator`` that
  guarantees no :class:`AppConfig` (and therefore no route with a dangling
  target) can ever be *constructed*, whatever the entry point.
* :func:`load_config` — validates the parsed mapping up front and raises a
  **plain** :class:`ValueError`. Pydantic converts exceptions raised inside a
  validator into ``ValidationError``; because the spec requires the loader to
  surface a plain ``ValueError`` that is not "converted into something else",
  the check is repeated on the raw mapping before model construction.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Mapping, TypeAlias

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "AppConfig",
    "GatewayServerConfig",
    "ProviderConfig",
    "RouteConfig",
    "RouteTargetConfig",
    "RoutingProfile",
    "load_config",
]

RoutingProfile: TypeAlias = Literal["budget", "balanced", "performance"]


class GatewayServerConfig(BaseModel):
    """HTTP server settings for the gateway process itself."""

    model_config = ConfigDict(extra="forbid")

    host: str = Field(
        default="127.0.0.1",
        description="Interface the gateway HTTP server binds to.",
    )
    port: int = Field(
        default=8000,
        ge=1,
        le=65535,
        description="TCP port the gateway HTTP server listens on.",
    )
    log_level: str = Field(
        default="info",
        description="Log verbosity for the gateway (e.g. 'debug', 'info', 'warning').",
    )


class ProviderConfig(BaseModel):
    """Connection details for a single upstream LLM provider."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["sharellm", "openai"] = Field(
        description="Wire protocol / adapter family used to talk to this provider.",
    )
    base_url: str = Field(
        description="Base URL of the provider's OpenAI-compatible API.",
    )
    api_key: str = Field(
        description="Credential used to authenticate against the provider.",
    )


class RouteTargetConfig(BaseModel):
    """A single upstream attempt within a route's ordered target list."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(
        description="Key into the top-level 'providers' map that serves this target.",
    )
    model: str = Field(
        description="Upstream model identifier to request from the provider.",
    )
    timeout_seconds: float = Field(
        default=15.0,
        gt=0,
        description="Per-request timeout, in seconds, for this target.",
    )


class RouteConfig(BaseModel):
    """A named routing rule mapping model aliases onto ordered targets."""

    model_config = ConfigDict(extra="forbid")

    models: list[str] = Field(
        min_length=1,
        description="Model aliases that this route is responsible for serving.",
    )
    strategy: str = Field(
        default="failover",
        description="Routing strategy used across this route's targets.",
    )
    targets: list[RouteTargetConfig] = Field(
        default_factory=list,
        description="Ordered list of upstream targets; earlier entries are tried first.",
    )
    routing_profile: RoutingProfile = "balanced"
    channel_targets: dict[str, RouteTargetConfig] = Field(default_factory=dict)
    match_limit: int = Field(default=3, gt=0, strict=True)

    @model_validator(mode="after")
    def _validate_strategy_targets(self) -> RouteConfig:
        if self.strategy == "matchmaker":
            if not self.channel_targets:
                raise ValueError("Matchmaker routes require nonempty channel_targets")
        elif not self.targets:
            raise ValueError("Static routes require at least one target")
        return self


class AppConfig(BaseModel):
    """Root configuration object for the ShareLLM Smart Gateway."""

    model_config = ConfigDict(extra="forbid")

    gateway: GatewayServerConfig = Field(
        default_factory=GatewayServerConfig,
        description="HTTP server settings for the gateway process.",
    )
    providers: dict[str, ProviderConfig] = Field(
        default_factory=dict,
        description="Named upstream providers, keyed by provider name.",
    )
    routes: dict[str, RouteConfig] = Field(
        default_factory=dict,
        description="Named routing rules, keyed by route name.",
    )

    registry_db_path: str = "data/registry.db"

    @model_validator(mode="after")
    def _validate_provider_references(self) -> AppConfig:
        """Reject a config whose routes reference providers that do not exist.

        Guarantees that no :class:`AppConfig` with a dangling ``target.provider``
        reference can be constructed through *any* entry point, including direct
        ``AppConfig(...)`` / ``AppConfig.model_validate(...)`` calls.

        Raises:
            ValueError: If a route target references an unknown provider.
        """
        _assert_provider_references(self.providers, self.routes)
        return self


def _unknown_provider_message(
    route_name: str, provider: str, known_providers: list[str]
) -> str:
    """Build the descriptive error message for a dangling provider reference."""
    return (
        f"Route '{route_name}' target references unknown provider '{provider}'. "
        f"Known providers: {known_providers}."
    )


def _assert_provider_references(
    providers: Mapping[str, ProviderConfig],
    routes: Mapping[str, RouteConfig],
) -> None:
    """Ensure every route target's provider exists in the provider map.

    Raises:
        ValueError: On the first dangling reference found.
    """
    known_providers = sorted(providers)
    for route_name, route in routes.items():
        for target in [*route.targets, *route.channel_targets.values()]:
            if target.provider not in providers:
                raise ValueError(
                    _unknown_provider_message(route_name, target.provider, known_providers)
                )


def _assert_raw_provider_references(raw: Mapping[str, Any]) -> None:
    """Reference check over the *unvalidated* YAML mapping.

    Runs before model construction so that :func:`load_config` can raise a plain
    :class:`ValueError` (pydantic would otherwise wrap a validator exception in
    ``ValidationError``). Anything this pass cannot decide unambiguously is left
    to the subsequent ``model_validate`` call, which reports the accurate
    structural/type error.

    Raises:
        ValueError: On the first dangling reference found.
    """
    providers_raw = raw.get("providers")
    if providers_raw is None:
        # Section omitted: any referenced provider is genuinely undeclared.
        known_providers: list[str] = []
    elif isinstance(providers_raw, Mapping):
        # ``key=str`` keeps this sort total even when the YAML mixes key types
        # (e.g. ``1:`` and ``a:``); the result is only used to build the message.
        known_providers = sorted(str(key) for key in providers_raw)
    else:
        # Structurally invalid (e.g. a list): let ``model_validate`` report the
        # accurate error instead of guessing at provider names here.
        return

    routes_raw = raw.get("routes")
    if not isinstance(routes_raw, Mapping):
        return

    for route_name, route_raw in routes_raw.items():
        if not isinstance(route_raw, Mapping):
            continue
        targets_raw = route_raw.get("targets")
        bindings_raw = route_raw.get("channel_targets")
        candidates = list(targets_raw) if isinstance(targets_raw, list) else []
        if isinstance(bindings_raw, Mapping):
            candidates.extend(bindings_raw.values())
        for target_raw in candidates:
            if not isinstance(target_raw, Mapping):
                continue
            provider = target_raw.get("provider")
            if not isinstance(provider, str):
                # Missing, null or non-string provider is a required-field or
                # type error; let ``model_validate`` report it accurately rather
                # than mislabelling it as a dangling reference.
                continue
            if provider not in known_providers:
                raise ValueError(
                    _unknown_provider_message(str(route_name), provider, known_providers)
                )


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    """Load and validate a gateway configuration from a YAML file.

    Args:
        path: Path to the YAML configuration file. Defaults to ``config.yaml``
            in the current working directory.

    Returns:
        A fully validated :class:`AppConfig`.

    Raises:
        FileNotFoundError: If ``path`` does not point at an existing file.
        ValueError: If the file is empty, does not contain a top-level mapping,
            or a route target references an unknown provider.
    """
    config_path = Path(path)
    if not config_path.is_file():
        raise FileNotFoundError(
            f"Configuration file not found: {config_path.resolve()} "
            f"(expected an existing YAML file)."
        )

    with config_path.open("r", encoding="utf-8") as handle:
        raw: object = yaml.safe_load(handle)

    if raw is None:
        raise ValueError(
            f"Configuration file {config_path.resolve()} is empty or contains "
            f"only comments; expected a YAML mapping with 'gateway', "
            f"'providers' and 'routes' sections."
        )

    if not isinstance(raw, Mapping):
        raise ValueError(
            f"Configuration file {config_path.resolve()} must contain a top-level "
            f"YAML mapping, but parsed to {type(raw).__name__}."
        )

    _assert_raw_provider_references(raw)
    config = AppConfig.model_validate(raw)
    # All relative registry paths (including the default) are config-relative.
    db_path = Path(config.registry_db_path)
    if not db_path.is_absolute():
        config.registry_db_path = str((config_path.resolve().parent / db_path).resolve())
    return config
