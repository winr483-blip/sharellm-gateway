"""Tests for :mod:`gateway.config` — the typed configuration engine."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from gateway.config import AppConfig, load_config

# Resolve the reference config relative to this test file so the suite is
# robust to whatever working directory the test runner happens to use.
EXAMPLE_CONFIG_PATH: Path = Path(__file__).resolve().parents[1] / "config.example.yaml"


def test_example_config_loads_and_resolves_references() -> None:
    """`config.example.yaml` loads into a valid AppConfig with resolved targets."""
    config = load_config(EXAMPLE_CONFIG_PATH)

    assert isinstance(config, AppConfig)

    # Gateway section: concrete defaults are present and typed.
    assert config.gateway.host == "127.0.0.1"
    assert config.gateway.port == 8000
    assert config.gateway.log_level == "info"

    # Both providers are declared and typed correctly.
    assert len(config.providers) == 2
    assert set(config.providers) == {"sharellm_primary", "openrouter_backup"}
    assert config.providers["sharellm_primary"].type == "sharellm"
    assert config.providers["openrouter_backup"].type == "openai"
    assert config.providers["openrouter_backup"].base_url == "https://openrouter.ai/api/v1"

    # The coding route preserves its specified alias list, strategy and order.
    assert "coding_route" in config.routes
    coding_route = config.routes["coding_route"]
    assert coding_route.models == ["gpt-6.1-sol", "gpt-6-astra"]
    assert coding_route.strategy == "failover"
    assert [target.provider for target in coding_route.targets] == [
        "sharellm_primary",
        "openrouter_backup",
    ]
    assert coding_route.targets[0].model == "gpt-6.1-sol"
    assert coding_route.targets[0].timeout_seconds == 15.0

    # A second route is present, exercising the multi-route shape.
    assert len(config.routes) == 2
    assert "chat_route" in config.routes

    # Every target provider reference resolves to a real provider entry.
    for route in config.routes.values():
        for target in route.targets:
            assert target.provider in config.providers


def test_unknown_provider_reference_raises(tmp_path: Path) -> None:
    """A route target pointing at a non-existent provider raises ValueError."""
    broken_config = tmp_path / "broken.yaml"
    broken_config.write_text(
        """
gateway:
  host: 127.0.0.1
  port: 8000
  log_level: info

providers:
  sharellm_primary:
    type: sharellm
    base_url: https://api.sharellm.example.com/v1
    api_key: sk-sharellm-REPLACE_WITH_REAL_KEY

routes:
  coding_route:
    models:
      - "gpt-6.1-sol"
    strategy: failover
    targets:
      - provider: ghost_provider
        model: "gpt-6.1-sol"
        timeout_seconds: 15.0
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as excinfo:
        load_config(broken_config)

    raised = excinfo.value

    # The loader must surface a *plain* ValueError, not pydantic's
    # ValidationError (which is a ValueError subclass and would otherwise make
    # `pytest.raises(ValueError)` pass even if only the model validator fired).
    assert type(raised) is ValueError, f"expected a plain ValueError, got {type(raised)!r}"
    assert not isinstance(raised, ValidationError)

    message = str(raised)
    assert "coding_route" in message
    assert "ghost_provider" in message
    assert "sharellm_primary" in message


def test_minimal_config_applies_documented_defaults(tmp_path: Path) -> None:
    """A minimal YAML gets every documented default applied exactly.

    The file omits the whole ``gateway:`` block, ``strategy`` and
    ``timeout_seconds``, so this guards the spec's defaults against a future
    refactor turning a ``Field(default=...)`` into a required field.
    """
    minimal_config = tmp_path / "minimal.yaml"
    minimal_config.write_text(
        """
providers:
  only_provider:
    type: openai
    base_url: https://api.example.com/v1
    api_key: sk-example-REPLACE_ME

routes:
  only_route:
    models:
      - "some-model"
    targets:
      - provider: only_provider
        model: "some-model"
""",
        encoding="utf-8",
    )

    config = load_config(minimal_config)

    # gateway block omitted entirely -> GatewayServerConfig defaults apply.
    assert config.gateway.host == "127.0.0.1"
    assert config.gateway.port == 8000
    assert config.gateway.log_level == "info"

    # route-level defaults.
    assert config.routes["only_route"].strategy == "failover"
    assert config.routes["only_route"].targets[0].timeout_seconds == 15.0

    # the defaults keep their declared types (int / float), not just their values.
    assert isinstance(config.gateway.port, int)
    assert isinstance(config.routes["only_route"].targets[0].timeout_seconds, float)
