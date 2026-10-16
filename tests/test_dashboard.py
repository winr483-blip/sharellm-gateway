"""Hermetic Task 2.3 dashboard acceptance tests.

Covers exactly the four user-requested dashboard acceptance cases:

1. ``GET /dashboard`` and ``GET /`` both serve the packaged HTML.
2. ``GET /v1/dashboard/stats`` clean-app zero state and legal profile.
3. ``GET /v1/dashboard/channels`` lists real persisted registry cards.
4. ``POST /v1/dashboard/profile`` returns updated state and preserves config.

All upstream/provider URLs, prices and metrics below are synthetic test inputs;
no real network call is made and no marketplace measurement is asserted. Tests
use the real ``create_app`` ASGI application with its lifespan and httpx's
``ASGITransport`` against temporary YAML config and a temporary SQLite registry.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import pytest
import yaml

from gateway.engine.registry import ChannelNode, ChannelRegistryStorage
from gateway.engine.router import CooldownRegistry
from gateway.main import create_app

pytestmark = pytest.mark.asyncio

_REPO = Path(__file__).resolve().parents[1]
_UI_FILE = _REPO / "src" / "gateway" / "ui" / "dashboard.html"


def _dynamic_config() -> dict[str, Any]:
    """A minimal matchmaker config; providers/URLs/keys are synthetic."""
    return {
        "registry_db_path": "channels.db",
        "gateway": {"host": "127.0.0.1", "port": 8000},
        "providers": {
            "p": {"type": "openai", "base_url": "https://p.test/v1", "api_key": "synthetic"},
            "q": {"type": "openai", "base_url": "https://q.test/v1", "api_key": "synthetic"},
        },
        "routes": {
            "smart": {
                "models": ["alias"],
                "strategy": "matchmaker",
                "routing_profile": "balanced",
                "channel_targets": {
                    "card-a": {"provider": "p", "model": "wire-a"},
                    "card-b": {"provider": "q", "model": "wire-b"},
                },
            }
        },
    }


def _write_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(_dynamic_config()), encoding="utf-8")
    return path


def _node(channel_id: str, **overrides: Any) -> ChannelNode:
    data: dict[str, Any] = {
        "channel_id": channel_id,
        "model_name": "alias",
        "publisher": f"pub-{channel_id}",
        "is_certified": True,
        "pricing": {
            "input_price_per_1m": 10.0,
            "cache_read_price_per_1m": 1.0,
            "cache_write_price_per_1m": 99.0,
            "output_price_per_1m": 99.0,
            "multiplier": 1.0,
        },
        "capabilities": {"supports_vision": True, "supports_tools": True, "context_window": 1_024_000},
        "authenticity": {"fingerprint_status": "matched"},
        "metrics": {
            "first_token_latency": 1.25,
            "tps": 42.5,
            "cache_hit_rate": 0.0,
            "sla_realtime": 99.5,
            "sla_24h": 98.0,
            "sla_7d": 97.0,
            "community_hearts": 7,
        },
    }
    data.update(overrides)
    return ChannelNode.model_validate(data)


async def _seed(tmp_path: Path, *nodes: ChannelNode) -> None:
    storage = ChannelRegistryStorage(tmp_path / "channels.db")
    await storage.initialize()
    try:
        for node in nodes:
            await storage.upsert_channel(node)
    finally:
        await storage.close()


@asynccontextmanager
async def _client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as client:
        yield client


async def test_dashboard_html_served_from_packaged_file(tmp_path: Path) -> None:
    app = create_app(_write_config(tmp_path))
    packaged = _UI_FILE.read_bytes()
    assert packaged

    async with app.router.lifespan_context(app):
        async with _client(app) as client:
            responses = [await client.get("/dashboard"), await client.get("/")]

    for response in responses:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        # Real served HTML is byte-identical to the packaged dashboard file.
        assert response.content == packaged

    # Both routes must serve the same document.
    assert responses[0].content == responses[1].content
    html = responses[0].text
    assert "<!doctype html>" in html.lower()
    # Real brand markers present in the shipped HUD markup.
    assert "ShareLLM" in html
    assert "HUD" in html
    assert "Glass HUD" in html


async def test_stats_clean_app_zero_state_and_legal_profile(tmp_path: Path) -> None:
    app = create_app(_write_config(tmp_path))

    async with app.router.lifespan_context(app):
        async with _client(app) as client:
            response = await client.get("/v1/dashboard/stats")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    data = response.json()
    assert isinstance(data, dict)

    for key in ("total_requests", "total_tokens", "cached_tokens", "cache_savings_usd", "active_profile"):
        assert key in data, key

    for key in ("total_requests", "total_tokens", "cached_tokens"):
        assert type(data[key]) is int  # strict int, never bool/float/str
        assert data[key] >= 0
    # A clean app has recorded nothing yet.
    assert data["total_requests"] == 0
    assert data["total_tokens"] == 0
    assert data["cached_tokens"] == 0

    savings = data["cache_savings_usd"]
    assert isinstance(savings, (int, float)) and not isinstance(savings, bool)
    assert savings >= 0
    assert float(savings) == 0.0

    assert data["active_profile"] in ("budget", "balanced", "performance")
    assert data["active_profile"] == "balanced"  # configured profile preserved
    assert data["global_profile_override"] is False
    assert data["recent_failovers"] == []
    assert "Process-local" in data["scope"]


async def test_channels_lists_real_persisted_cards_with_metrics(tmp_path: Path) -> None:
    await _seed(
        tmp_path,
        _node("card-a"),
        _node(
            "card-b",
            authenticity={"fingerprint_status": "differs"},
            metrics={
                "first_token_latency": 3.5,
                "tps": None,
                "cache_hit_rate": 0.0,
                "sla_realtime": 95.0,
                "sla_24h": 94.0,
                "sla_7d": 93.0,
                "community_hearts": 0,
            },
        ),
    )
    app = create_app(_write_config(tmp_path))

    async with app.router.lifespan_context(app):
        async with _client(app) as client:
            first = await client.get("/v1/dashboard/channels")
            assert first.status_code == 200
            payload = first.json()
            # Documented envelope: {"channels": [...], "metrics_scope": ..., "pricing_note": ...}
            assert isinstance(payload, dict)
            assert isinstance(payload["channels"], list)
            assert payload["metrics_scope"]
            assert payload["pricing_note"]

            rows = {row["channel_id"]: row for row in payload["channels"]}
            assert list(rows) == ["card-a", "card-b"]  # deterministic channel_id order

            a = rows["card-a"]
            assert (a["model_name"], a["publisher"], a["is_certified"]) == ("alias", "pub-card-a", True)
            assert (a["first_token_latency"], a["tps"]) == (1.25, 42.5)
            assert (a["sla_realtime"], a["sla_24h"], a["sla_7d"]) == (99.5, 98.0, 97.0)
            assert a["community_hearts"] == 7
            assert a["fingerprint_status"] == "matched"
            assert a["bound"] is True
            assert a["cooldown"] is False

            b = rows["card-b"]
            assert b["fingerprint_status"] == "differs"
            assert b["tps"] is None
            assert (b["first_token_latency"], b["community_hearts"]) == (3.5, 0)
            assert b["bound"] is True
            assert b["cooldown"] is False

            # Latest persisted metrics: upsert through the live storage, re-read
            # via the endpoint and confirm the updated values are served.
            updated = _node(
                "card-a",
                metrics={
                    "first_token_latency": 0.5,
                    "tps": 100.0,
                    "cache_hit_rate": 0.0,
                    "sla_realtime": 100.0,
                    "sla_24h": 99.0,
                    "sla_7d": 98.0,
                    "community_hearts": 9,
                },
            )
            await app.state.channel_registry.upsert_channel(updated)
            reread = {row["channel_id"]: row for row in (await client.get("/v1/dashboard/channels")).json()["channels"]}
            a2 = reread["card-a"]
            assert (a2["first_token_latency"], a2["tps"]) == (0.5, 100.0)
            assert (a2["sla_realtime"], a2["sla_24h"], a2["sla_7d"]) == (100.0, 99.0, 98.0)
            assert a2["community_hearts"] == 9

            # Cooldown reflects the real (injected-clock) breaker, not a stub.
            app.state.cooldown_registry = CooldownRegistry(clock=lambda: 0)
            app.state.cooldown_registry.record_failure(
                app.state.app_config.routes["smart"].channel_targets["card-a"]
            )
            cooled = {row["channel_id"]: row for row in (await client.get("/v1/dashboard/channels")).json()["channels"]}
            assert cooled["card-a"]["cooldown"] is True
            assert cooled["card-b"]["cooldown"] is False


async def test_profile_switch_returns_state_and_preserves_configured_route(tmp_path: Path) -> None:
    app = create_app(_write_config(tmp_path))

    async with app.router.lifespan_context(app):
        async with _client(app) as client:
            initial = (await client.get("/v1/dashboard/stats")).json()
            assert initial["active_profile"] == "balanced"
            assert initial["global_profile_override"] is False

            performance = await client.post("/v1/dashboard/profile", json={"profile": "performance"})
            assert performance.status_code == 200
            assert performance.json() == {"active_profile": "performance"}
            after_performance = (await client.get("/v1/dashboard/stats")).json()
            assert after_performance["active_profile"] == "performance"
            assert after_performance["global_profile_override"] is True

            budget = await client.post("/v1/dashboard/profile", json={"profile": "budget"})
            assert budget.status_code == 200
            assert budget.json() == {"active_profile": "budget"}
            after_budget = (await client.get("/v1/dashboard/stats")).json()
            assert after_budget["active_profile"] == "budget"

            # Effective future behavior is a captured per-request override; the
            # configured route definition itself is never mutated.
            assert app.state.dashboard_telemetry.profile_override == "budget"
            assert app.state.app_config.routes["smart"].routing_profile == "balanced"
