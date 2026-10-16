"""Hermetic Task2.2 acceptance tests; all marketplace numbers are synthetic."""
from __future__ import annotations

import copy
import json
import math
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
import respx
import yaml
from pydantic import ValidationError

from gateway.config import AppConfig, RouteConfig, RouteTargetConfig, load_config
from gateway.engine.registry.matcher import (
    ChannelMatcher, ChannelScorer, RoutingProfile,
    effective_input_price, estimate_input_tokens, has_image_url,
    normalize_lower, profile_weights, required_tokens, score_channels,
)
from gateway.engine.registry import ChannelNode, ChannelRegistryStorage
from gateway.engine.router import CooldownRegistry, dispatch
from gateway.main import create_app


def node(channel_id: str = "a", *, price: float = 10, cache: float = 0,
         read: float = 1, multiplier: float = 1, latency: float = 2,
         status: str = "matched", context: int = 1_024_000, vision: bool = True,
         hearts: int = 0, sla: tuple[float, float, float] = (100, 100, 100),
         model: str = "alias") -> ChannelNode:
    return ChannelNode.model_validate({
        "channel_id": channel_id, "model_name": model, "publisher": "untrusted-not-a-provider",
        "is_certified": True,
        "pricing": {"input_price_per_1m": price, "cache_read_price_per_1m": read,
                    "cache_write_price_per_1m": 999, "output_price_per_1m": 999,
                    "multiplier": multiplier},
        "capabilities": {"supports_vision": vision, "supports_tools": True, "context_window": context},
        "authenticity": {"fingerprint_status": status},
        "metrics": {"first_token_latency": latency, "cache_hit_rate": cache,
                    "sla_realtime": sla[0], "sla_24h": sla[1], "sla_7d": sla[2],
                    "community_hearts": hearts},
    })


def bindings(*ids: str) -> dict[str, RouteTargetConfig]:
    return {key: RouteTargetConfig(provider="p", model=f"upstream-{key}") for key in ids}


@pytest_asyncio.fixture
async def storage(tmp_path: Path):
    result = ChannelRegistryStorage(tmp_path / "channels.db")
    await result.initialize()
    try:
        yield result
    finally:
        await result.close()


async def seed(storage: ChannelRegistryStorage, *nodes: ChannelNode) -> None:
    for channel in nodes:
        await storage.upsert_channel(channel)


@pytest.mark.asyncio
async def test_budget_cache_and_performance_verified(storage):
    await seed(storage, node("cheap", cache=.91, price=10, read=.1, latency=4),
               node("fast", price=12, latency=1.2),
               node("differs", price=0, latency=.01, status="differs"),
               node("untested", price=0, latency=.001, status="untested"))
    matcher = ChannelMatcher(storage, bindings("cheap", "fast", "differs", "untested"))
    # Restrict budget universe to the two usable example bindings.
    budget = ChannelMatcher(storage, bindings("cheap", "fast"))
    assert [t.model for t in await budget.select_targets("alias", "budget", {})] == ["upstream-cheap", "upstream-fast"]
    assert [t.model for t in await matcher.select_targets("alias", "performance", {})] == ["upstream-fast", "upstream-cheap"]
    assert effective_input_price(node(cache=.91, price=10, read=.1, multiplier=2)) == Decimal("1.982")


@pytest.mark.parametrize("profile,expected", [
    ("budget", (.60, .15, .15, .10)),
    ("balanced", (.35, .35, .20, .10)),
    ("performance", (.10, .50, .30, .10)),
])
def test_public_channel_scorer_api(profile: RoutingProfile, expected):
    scorer = ChannelScorer()
    channels = [node("cheap", price=1, latency=4), node("fast", price=3, latency=1)]
    original = copy.deepcopy(channels)
    assert callable(scorer.score)
    assert scorer.weights(profile) == expected
    assert scorer.score(channels, profile) == score_channels(channels, profile)
    assert scorer.score([], profile) == []
    assert channels == original


@pytest.mark.asyncio
async def test_matcher_uses_owned_channel_scorer(storage, monkeypatch):
    await seed(storage, node("a", price=1), node("b", price=2), node("unbound"))
    matcher = ChannelMatcher(storage, bindings("a", "b"))
    observed = []
    original_score = ChannelScorer.score
    def spy(self, channels, profile):
        observed.append((self, [channel.channel_id for channel in channels], profile))
        return original_score(self, channels, profile)
    monkeypatch.setattr(ChannelScorer, "score", spy)
    assert [target.model for target in await matcher.select_targets("alias", "budget", {})] == ["upstream-a", "upstream-b"]
    assert len(observed) == 1
    assert isinstance(observed[0][0], ChannelScorer)
    assert observed[0][1:] == (["a", "b"], "budget")


def test_exact_balanced_components_and_profile_weights():
    assert profile_weights("budget") == (.60, .15, .15, .10)
    assert profile_weights("balanced") == (.35, .35, .20, .10)
    assert profile_weights("performance") == (.10, .50, .30, .10)
    scores = {s.channel_id: s for s in score_channels([
        node("a", price=1, latency=3, sla=(60, 90, 30), hearts=10),
        node("b", price=3, latency=1, hearts=20),
    ], "balanced")}
    a = scores["a"]
    assert (a.price, a.latency, a.sla, a.hearts) == (100, 0, 60, 50)
    assert a.total == pytest.approx(35 + 12 + 5)
    assert scores["b"].total == pytest.approx(35 + 20 + 10)


@pytest.mark.parametrize("profile", ["budget", "balanced", "performance"])
def test_bounds_ties_zero_ranges_free_and_extremes(profile):
    channels = [node("z", price=0), node("a", price=0)]
    scores = score_channels(channels, profile)
    assert [s.channel_id for s in scores] == ["a", "z"]
    assert all((s.price, s.latency, s.hearts) == (100, 100, 0) for s in scores)
    huge = node("huge", price=1e308, multiplier=1e308, latency=1e308, hearts=10**1000)
    assert effective_input_price(huge).is_finite()
    extreme = score_channels([node("free", price=0, latency=0), huge], profile)
    assert all(math.isfinite(s.total) and 0 <= s.total <= 100 for s in extreme)
    assert normalize_lower([Decimal(0), Decimal("1e616")]) == [100, 0]
    assert normalize_lower([]) == []


@pytest.mark.parametrize("value", [None, "", {"url": "data:image/png;base64,AA=="}, [1]])
def test_image_key_recursive_regardless_of_value(value):
    assert has_image_url({"messages": [{"content": [{"nested": {"image_url": value}}]}]})
    assert not has_image_url({"messages": [{"content": "image_url is just text"}]})


@pytest.mark.asyncio
async def test_images_context_and_real_200k_content(storage):
    await seed(storage, node("small", context=128_000), node("large"), node("blind", vision=False))
    matcher = ChannelMatcher(storage, bindings("small", "large", "blind"))
    image = {"messages": [{"content": [{"type": "image_url", "image_url": {"url": "https://no-fetch.invalid/image"}}]}]}
    assert {t.model for t in await matcher.select_targets("alias", "budget", image)} == {"upstream-small", "upstream-large"}
    payload = {"messages": [{"role": "user", "content": "x" * 800_004}], "estimated_tokens": 1}
    assert estimate_input_tokens(payload) > 200_000
    assert {t.model for t in await matcher.select_targets("alias", "balanced", payload)} == {"upstream-large", "upstream-blind"}
    injected = ChannelMatcher(storage, bindings("small", "large"), token_estimator=lambda _: 200_000)
    assert [t.model for t in await injected.select_targets("alias", "budget", {})] == ["upstream-large"]


def test_estimator_structure_utf8_tools_reservation_and_nonmutation():
    payload = {"messages": [{"content": "你好"}], "tools": [{"function": {"name": "f", "description": "x" * 200}}],
               "input": "another prompt", "max_tokens": 20, "max_completion_tokens": 30, "max_output_tokens": 10}
    original = copy.deepcopy(payload)
    estimated = estimate_input_tokens(payload)
    expected = json.dumps({key: payload[key] for key in ("messages", "input", "tools")}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert estimated == (len(expected) + 3) // 4
    assert required_tokens(payload, estimate_input_tokens) == estimated + 30
    assert estimate_input_tokens({**payload, "estimated_tokens": -999, "metadata": "x" * 10000}) == estimated
    assert payload == original


@pytest.mark.parametrize("value", [-1, True, 1.2, "100", None])
@pytest.mark.parametrize("key", ["max_tokens", "max_completion_tokens", "max_output_tokens"])
def test_output_token_validation(key, value):
    with pytest.raises(ValueError, match="nonnegative integer"):
        required_tokens({key: value}, lambda _: 0)


@pytest.mark.asyncio
async def test_reservation_boundary_copies_dedupe_bindings_and_model(storage):
    await seed(storage, node("a", context=100), node("b", context=100), node("c", context=100),
               node("unbound", price=1e308), node("wrong", model="other"))
    targets = bindings("a", "b", "c", "wrong")
    targets["b"] = targets["a"].model_copy()
    matcher = ChannelMatcher(storage, targets, token_estimator=lambda _: 80)
    targets["a"].model = "changed-after-construction"
    payload = {"max_tokens": 20, "messages": [{"content": "preserve me"}]}
    original = copy.deepcopy(payload)
    result = await matcher.select_targets("alias", "balanced", payload)
    assert [t.model for t in result] == ["upstream-a", "upstream-c"]
    result[0].model = "changed-result"
    assert (await matcher.select_targets("alias", "balanced", payload))[0].model == "upstream-a"
    assert payload == original
    assert await matcher.select_targets("alias", "balanced", {"max_tokens": 21}) == []
    assert await matcher.select_targets("missing", "balanced", {}) == []
    assert len(await matcher.select_targets("alias", "balanced", {}, limit=1)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, -1, True, 1.2, "3", None])
async def test_invalid_limit(storage, limit):
    with pytest.raises(ValueError, match="positive integer"):
        await ChannelMatcher(storage, {}).select_targets("alias", "budget", {}, limit)


@pytest.mark.parametrize("estimate", [-1, True, 1.5, "5", None])
def test_invalid_injected_estimator(estimate):
    with pytest.raises(ValueError, match="Token estimator"):
        required_tokens({}, lambda _: estimate)


@pytest.mark.asyncio
async def test_unbound_channel_cannot_change_normalization(storage):
    await seed(storage, node("cheap", price=1, latency=10), node("fast", price=2, latency=1),
               node("unbound", price=1e300, latency=1))
    matcher = ChannelMatcher(storage, bindings("cheap", "fast"))
    assert [t.model for t in await matcher.select_targets("alias", "budget", {})] == ["upstream-cheap", "upstream-fast"]
    # Including the unbound price outlier would collapse the price difference,
    # incorrectly making the fast channel win despite budget weighting.
    assert score_channels([node("cheap", price=1, latency=10), node("fast", price=2, latency=1), node("unbound", price=1e300, latency=1)], "budget")[0].channel_id == "fast"


@pytest.mark.asyncio
async def test_defensive_filters_and_query_arguments(storage):
    storage.get_channels_for_model = AsyncMock(return_value=[
        node("bound", context=300_000), node("small", context=128_000),
        node("blind", vision=False), node("bad", status="differs"),
        node("wrong", model="other"), node("unbound"),
    ])
    matcher = ChannelMatcher(storage, bindings("bound", "small", "blind", "bad", "wrong"), lambda _: 200_000)
    payload = {"messages": [{"image_url": None}]}
    assert [t.model for t in await matcher.select_targets("alias", "performance", payload)] == ["upstream-bound"]
    storage.get_channels_for_model.assert_awaited_once_with("alias", min_context=200_000, vision_required=True, strict_authenticity=True)


def dynamic_config() -> dict[str, Any]:
    return {"registry_db_path": "channels.db", "providers": {
        "p": {"type": "openai", "base_url": "https://p.test/v1", "api_key": "synthetic"},
        "q": {"type": "openai", "base_url": "https://q.test/v1", "api_key": "synthetic"}},
        "routes": {"smart": {"models": ["alias"], "strategy": "matchmaker", "routing_profile": "budget",
                              "channel_targets": {"a": {"provider": "p", "model": "wire-a"}, "b": {"provider": "q", "model": "wire-b"}}}}}


def test_dynamic_yaml_and_invalid_configuration(tmp_path):
    raw = dynamic_config()
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    config = load_config(path)
    assert config.routes["smart"].targets == []
    assert config.routes["smart"].match_limit == 3
    assert Path(config.registry_db_path) == tmp_path / "channels.db"
    raw["routes"]["smart"]["channel_targets"]["a"]["provider"] = "ghost"
    with pytest.raises(ValidationError, match="unknown provider"):
        AppConfig.model_validate(raw)
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        load_config(path)
    assert type(exc.value) is ValueError
    for invalid in ({"strategy": "failover"}, {"strategy": "other", "targets": []},
                    {"strategy": "matchmaker"}, {"strategy": "matchmaker", "channel_targets": {}}):
        with pytest.raises(ValidationError):
            RouteConfig.model_validate({"models": ["alias"], **invalid})
    for key, value in (("routing_profile", "fastest"), ("match_limit", 0), ("match_limit", True)):
        data = dynamic_config()
        data["routes"]["smart"][key] = value
        with pytest.raises(ValidationError):
            AppConfig.model_validate(data)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("mode", ["missing", "empty", "error", "unknown", "invalid"])
async def test_controlled_selection_errors(storage, stream, mode):
    config = AppConfig.model_validate(dynamic_config())
    route = config.routes["smart"]
    # A static list must never become an unsafe fallback.
    route.targets = [RouteTargetConfig(provider="p", model="unsafe-fallback")]
    matcher = ChannelMatcher(storage, route.channel_targets)
    if mode == "error":
        matcher.select_targets = AsyncMock(side_effect=RuntimeError("storage unavailable"))
    elif mode == "unknown":
        matcher.select_targets = AsyncMock(return_value=[RouteTargetConfig(provider="ghost", model="bad")])
    elif mode == "invalid":
        matcher.select_targets = AsyncMock(side_effect=ValueError("max_tokens must be a nonnegative integer"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("must not call upstream"))) as client:
        result, provider = await dispatch(client, route, config, {"model": "alias"}, stream, {}, CooldownRegistry(), None if mode == "missing" else matcher)
    assert result[0] == (400 if mode == "invalid" else 503)
    assert "error" in result[1]
    assert provider is None
    if stream:
        assert result[3] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_real_app_sqlite_ranking_failover_cooldown(tmp_path, stream):
    raw = dynamic_config()
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    storage = ChannelRegistryStorage(tmp_path / "channels.db")
    await storage.initialize()
    await seed(storage, node("a", price=10, read=.1, cache=.91, latency=4), node("b", price=12, latency=1.2))
    await storage.close()
    app = create_app(path)
    async with app.router.lifespan_context(app):
        # Deterministic clock proves cooldown without sleeping.
        app.state.cooldown_registry = CooldownRegistry(clock=lambda: 0)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://gateway.test") as client:
            with respx.mock(assert_all_called=False) as upstream:
                a = upstream.post("https://p.test/v1/chat/completions").mock(return_value=httpx.Response(200, json={"rank": 1}))
                b = upstream.post("https://q.test/v1/chat/completions").mock(return_value=httpx.Response(200, json={"rank": 2}))
                # Buffered success always proves rank1 was selected first.
                first = await client.post("/v1/chat/completions", json={"model": "alias", "messages": [{"content": "hello"}]})
                assert first.json() == {"rank": 1}
                assert a.call_count == 1 and b.call_count == 0
                a.mock(return_value=httpx.Response(429, json={"error": {"message": "rate limited"}}))
                sse = b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'
                if stream:
                    b.mock(return_value=httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"}))
                payload = {"model": "alias", "messages": [{"content": "preserved"}], "tools": [], "tool_choice": "none", "stream": stream}
                original = copy.deepcopy(payload)
                second = await client.post("/v1/chat/completions", json=payload)
                assert second.status_code == 200
                assert (second.content == sse) if stream else (second.json() == {"rank": 2})
                assert a.call_count == 2 and b.call_count == 1
                assert json.loads(a.calls[-1].request.content) == {"model": "wire-a", "messages": payload["messages"], "stream": stream}
                assert json.loads(b.calls[-1].request.content) == {"model": "wire-b", "messages": payload["messages"], "stream": stream}
                third = await client.post("/v1/chat/completions", json=payload)
                assert third.status_code == 200 and a.call_count == 2 and b.call_count == 2
                assert payload == original
                # If every resolved endpoint is cooling, retain soft-breaker
                # semantics: retry the full dynamically ranked list.
                app.state.cooldown_registry.record_failure(app.state.app_config.routes["smart"].channel_targets["b"])
                a.mock(return_value=httpx.Response(200, json={"rank": 1}))
                fourth = await client.post("/v1/chat/completions", json={"model": "alias"})
                assert fourth.json() == {"rank": 1}
                assert a.call_count == 3 and b.call_count == 2


@pytest.mark.asyncio
async def test_lifecycle_owned_storage_shared_and_closed_on_failure(tmp_path, monkeypatch):
    import gateway.main as main
    raw = dynamic_config()
    raw["routes"]["second"] = {**raw["routes"]["smart"], "models": ["second"]}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    created = []
    class TrackedStorage(ChannelRegistryStorage):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)
        async def close(self):
            await super().close()
            self.was_closed = True
    monkeypatch.setattr(main, "ChannelRegistryStorage", TrackedStorage)
    app = create_app(path)
    async with app.router.lifespan_context(app):
        assert len(created) == 1 and len(app.state.channel_matchers) == 2
    assert created[0].was_closed and app.state.http_client.is_closed
    async def broken_initialize(self):
        raise RuntimeError("startup failure")
    monkeypatch.setattr(TrackedStorage, "initialize", broken_initialize)
    failed = create_app(path)
    with pytest.raises(RuntimeError, match="startup failure"):
        async with failed.router.lifespan_context(failed):
            pytest.fail("startup cannot yield")
    assert created[-1].was_closed and failed.state.http_client.is_closed


@pytest.mark.asyncio
async def test_static_only_never_constructs_registry(tmp_path, monkeypatch):
    import gateway.main as main
    raw = dynamic_config()
    raw["routes"] = {"static": {"models": ["alias"], "targets": [{"provider": "p", "model": "wire"}]}}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setattr(main, "ChannelRegistryStorage", lambda *a, **kw: pytest.fail("static DB creation"))
    app = create_app(path)
    async with app.router.lifespan_context(app):
        assert app.state.channel_matchers == {}
        assert not (tmp_path / "channels.db").exists()
    assert app.state.http_client.is_closed
