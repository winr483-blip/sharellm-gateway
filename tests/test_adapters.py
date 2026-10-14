"""Adapter tests for the ShareLLM Smart Gateway (Task 1.4).

Covers the four task requirements — target model rewriting, ShareLLM error
normalization, prompt-cache telemetry, and the anti-422 parameter sanitizer —
plus the edge cases the implementation deliberately decides on (malformed
``usage``, zero ``prompt_tokens``, non-``share_llm_error`` passthrough, and a
payload with no tool fields surviving unchanged).

Everything runs in-process: the gateway is driven through
:class:`httpx.ASGITransport` (no socket is bound) and the upstream providers are
intercepted by :mod:`respx`, so no real network call is ever made. Every
expected URL and model is derived from ``config.example.yaml`` rather than
hard-coded.
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import pytest
import pytest_asyncio
import respx
from fastapi import FastAPI

from gateway.adapters.sanitizer import sanitize_request_payload
from gateway.adapters.sharellm import (
    SHARE_LLM_ERROR_TYPE,
    extract_cache_stats,
    format_cache_log,
    log_cache_stats,
    sanitize_error_envelope,
)
from gateway.config import load_config
from gateway.engine.router import CooldownRegistry, dispatch
from gateway.main import create_app

EXAMPLE_CONFIG_PATH: Path = Path(__file__).resolve().parents[1] / "config.example.yaml"

#: The reference configuration, loaded once for the whole module.
EXAMPLE_CONFIG = load_config(EXAMPLE_CONFIG_PATH)

#: ``coding_route`` and its two ordered targets (primary, backup).
CODING_ROUTE = EXAMPLE_CONFIG.routes["coding_route"]
PRIMARY_TARGET = CODING_ROUTE.targets[0]
BACKUP_TARGET = CODING_ROUTE.targets[1]

PRIMARY_PROVIDER = EXAMPLE_CONFIG.providers[PRIMARY_TARGET.provider]
BACKUP_PROVIDER = EXAMPLE_CONFIG.providers[BACKUP_TARGET.provider]

#: The chat-completions endpoints the gateway must POST to, derived from config.
PRIMARY_URL = PRIMARY_PROVIDER.base_url.rstrip("/") + "/chat/completions"
BACKUP_URL = BACKUP_PROVIDER.base_url.rstrip("/") + "/chat/completions"

GATEWAY_BASE_URL = "http://gateway.test"

#: A distinct success payload returned by the backup target.
BACKUP_SUCCESS_BODY: dict[str, Any] = {
    "id": "chatcmpl-backup",
    "object": "chat.completion",
    "model": "gpt-6-astra",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hello from the backup"},
            "finish_reason": "stop",
        }
    ],
}

#: A request body whose alias ("gpt-6-astra") is served by ``coding_route`` but
#: is *not* the primary target's model ("gpt-6.1-sol") — so a verbatim-forwarding
#: implementation would visibly forward the wrong ``model``.
ALIAS_PAYLOAD: dict[str, Any] = {
    "model": "gpt-6-astra",
    "messages": [{"role": "user", "content": "Hi"}],
    "stream": False,
}

#: The exact body the primary target must receive for ``ALIAS_PAYLOAD``.
PRIMARY_EXPECTED_BODY: dict[str, Any] = {
    "model": "gpt-6.1-sol",
    "messages": [{"role": "user", "content": "Hi"}],
    "stream": False,
}

#: The exact body the backup target must receive for ``ALIAS_PAYLOAD``.
BACKUP_EXPECTED_BODY: dict[str, Any] = {
    "model": "gpt-6-astra",
    "messages": [{"role": "user", "content": "Hi"}],
    "stream": False,
}


@dataclass
class GatewayHarness:
    """A lifespan-started gateway app plus a client wired to its ASGI surface."""

    app: FastAPI
    client: httpx.AsyncClient


@pytest_asyncio.fixture
async def gateway() -> AsyncIterator[GatewayHarness]:
    """Start the gateway's lifespan and yield an in-process ASGI client."""
    application = create_app(EXAMPLE_CONFIG_PATH)
    async with application.router.lifespan_context(application):
        assert isinstance(application.state.http_client, httpx.AsyncClient)
        assert isinstance(application.state.cooldown_registry, CooldownRegistry)
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport, base_url=GATEWAY_BASE_URL
        ) as client:
            yield GatewayHarness(app=application, client=client)


# --------------------------------------------------------------------------- #
# Required test 1 — target model rewriting
# --------------------------------------------------------------------------- #


def test_config_fixture_is_discriminating() -> None:
    """The fixture is only meaningful because alias != primary target model."""
    assert PRIMARY_TARGET.model == "gpt-6.1-sol"
    assert BACKUP_TARGET.model == "gpt-6-astra"
    assert ALIAS_PAYLOAD["model"] != PRIMARY_TARGET.model


@pytest.mark.asyncio
async def test_target_model_rewriting_uses_the_target_model(
    gateway: GatewayHarness,
) -> None:
    """An inbound alias is rewritten to the *target's* upstream model id.

    The inbound alias ``gpt-6-astra`` is served by ``coding_route``, whose
    primary target declares ``gpt-6.1-sol``. The gateway must forward
    ``gpt-6.1-sol`` upstream — not the alias — which is exactly what a
    verbatim-forwarding implementation would get wrong.
    """
    with respx.mock(assert_all_called=False) as mock:
        primary = mock.post(PRIMARY_URL).mock(
            return_value=httpx.Response(
                200,
                json=BACKUP_SUCCESS_BODY,
                headers={"content-type": "application/json"},
            )
        )

        response = await gateway.client.post(
            "/v1/chat/completions", json=ALIAS_PAYLOAD
        )

        assert primary.call_count == 1
        forwarded = json.loads(primary.calls.last.request.content)

    assert response.status_code == 200
    # The outbound body carries the target's model, and differs from the alias.
    assert forwarded["model"] == PRIMARY_TARGET.model == "gpt-6.1-sol"
    assert forwarded["model"] != ALIAS_PAYLOAD["model"]
    assert forwarded == PRIMARY_EXPECTED_BODY


@pytest.mark.asyncio
async def test_dispatch_rewrites_model_per_target_and_never_mutates_the_caller(
    gateway: GatewayHarness,
) -> None:
    """One failover rewrites both targets' bodies and leaves the caller's dict intact."""
    inbound = copy.deepcopy(ALIAS_PAYLOAD)
    snapshot = copy.deepcopy(inbound)
    registry: CooldownRegistry = gateway.app.state.cooldown_registry

    with respx.mock(assert_all_called=False) as mock:
        primary = mock.post(PRIMARY_URL).mock(
            return_value=httpx.Response(
                429,
                json={"error": {"message": "primary rate limited"}},
                headers={"content-type": "application/json"},
            )
        )
        backup = mock.post(BACKUP_URL).mock(
            return_value=httpx.Response(
                200,
                json=BACKUP_SUCCESS_BODY,
                headers={"content-type": "application/json"},
            )
        )

        (status_code, body, _headers), provider_name = await dispatch(
            gateway.app.state.http_client,
            CODING_ROUTE,
            EXAMPLE_CONFIG,
            inbound,
            False,
            {},
            registry,
        )

        # The primary attempt saw the primary target's model; the backup saw its
        # own. Both are concrete outbound bodies captured from respx.
        assert json.loads(primary.calls.last.request.content) == PRIMARY_EXPECTED_BODY
        assert json.loads(backup.calls.last.request.content) == BACKUP_EXPECTED_BODY

    assert status_code == 200
    assert body == BACKUP_SUCCESS_BODY
    assert provider_name == BACKUP_TARGET.provider == "openrouter_backup"

    # The rewrite lives on a per-target copy: the caller's dict is unchanged.
    assert inbound == snapshot
    assert inbound == ALIAS_PAYLOAD


# --------------------------------------------------------------------------- #
# Required test 2 — ShareLLM error normalization
# --------------------------------------------------------------------------- #


def test_share_llm_429_is_normalized_to_rate_limit_error() -> None:
    """A 429 ``share_llm_error`` becomes ``rate_limit_error``; message/code survive."""
    upstream = {
        "error": {
            "message": "Rate limit reached for gpt-6.1-sol",
            "type": SHARE_LLM_ERROR_TYPE,
            "code": "rate_limit_exceeded",
        }
    }

    sanitized = sanitize_error_envelope(429, upstream)

    assert sanitized == {
        "error": {
            "message": "Rate limit reached for gpt-6.1-sol",
            "type": "rate_limit_error",
            "code": "rate_limit_exceeded",
        }
    }
    # The caller's envelope is not mutated.
    assert upstream["error"]["type"] == SHARE_LLM_ERROR_TYPE


def test_share_llm_error_maps_400_and_5xx() -> None:
    """400 → invalid_request_error; any 5xx → api_error."""
    assert sanitize_error_envelope(
        400, {"error": {"message": "bad field", "type": SHARE_LLM_ERROR_TYPE}}
    ) == {"error": {"message": "bad field", "type": "invalid_request_error"}}
    assert sanitize_error_envelope(
        500, {"error": {"message": "boom", "type": SHARE_LLM_ERROR_TYPE}}
    ) == {"error": {"message": "boom", "type": "api_error"}}
    assert sanitize_error_envelope(
        503, {"error": {"message": "boom", "type": SHARE_LLM_ERROR_TYPE}}
    ) == {"error": {"message": "boom", "type": "api_error"}}


def test_rate_limit_message_overrides_the_status() -> None:
    """A "limit"/"overloaded" message is a rate-limit signal on any status."""
    assert (
        sanitize_error_envelope(
            503, {"error": {"message": "server overloaded", "type": SHARE_LLM_ERROR_TYPE}}
        )["error"]["type"]
        == "rate_limit_error"
    )
    assert (
        sanitize_error_envelope(
            400, {"error": {"message": "you hit the rate limit", "type": SHARE_LLM_ERROR_TYPE}}
        )["error"]["type"]
        == "rate_limit_error"
    )


def test_sanitize_error_envelope_leaves_non_share_llm_error_untouched() -> None:
    """A standard OpenAI error envelope (incl. the gateway's own) is never rewritten."""
    standard = {
        "error": {
            "message": "bad request",
            "type": "invalid_request_error",
            "code": None,
        }
    }
    assert sanitize_error_envelope(400, standard) == standard

    gateway_envelope = {
        "error": {
            "message": "All upstream targets failed",
            "type": "gateway_failover_exhausted",
        }
    }
    assert sanitize_error_envelope(429, gateway_envelope) == gateway_envelope


def test_sanitize_error_envelope_leaves_unmapped_status_untouched() -> None:
    """A ShareLLM error on a status the spec does not cover keeps its own type."""
    upstream = {
        "error": {
            "message": "unauthorized",
            "type": SHARE_LLM_ERROR_TYPE,
            "code": "invalid_api_key",
        }
    }
    for status_code in (401, 403, 404, 422):
        assert sanitize_error_envelope(status_code, upstream) == upstream


def test_sanitize_error_envelope_handles_malformed_envelopes() -> None:
    """Missing / non-mapping ``error`` and missing fields never raise."""
    # No `error` member at all.
    assert sanitize_error_envelope(429, {"detail": "boom"}) == {"detail": "boom"}
    # `error` is not a mapping.
    assert sanitize_error_envelope(429, {"error": "boom"}) == {"error": "boom"}
    # `error` present but without a `type`.
    assert sanitize_error_envelope(429, {"error": {"message": "boom"}}) == {
        "error": {"message": "boom"}
    }
    # `type` present but no `message` and no `code`: the type is normalized and
    # nothing is invented.
    assert sanitize_error_envelope(429, {"error": {"type": SHARE_LLM_ERROR_TYPE}}) == {
        "error": {"type": "rate_limit_error"}
    }
    # `code` present is preserved; `code: None` is preserved as None.
    assert sanitize_error_envelope(
        400, {"error": {"type": SHARE_LLM_ERROR_TYPE, "message": "bad"}}
    ) == {"error": {"type": "invalid_request_error", "message": "bad"}}
    assert sanitize_error_envelope(
        500,
        {"error": {"type": SHARE_LLM_ERROR_TYPE, "message": "boom", "code": None}},
    ) == {"error": {"type": "api_error", "message": "boom", "code": None}}


# --------------------------------------------------------------------------- #
# Required test 3 — cache telemetry extractor
# --------------------------------------------------------------------------- #


def test_extract_cache_stats_computes_the_ratio() -> None:
    """The spec's 3840/4387 example yields exactly 87.5%."""
    response_json = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "usage": {
            "prompt_tokens": 4387,
            "completion_tokens": 12,
            "total_tokens": 4399,
            "prompt_tokens_details": {"cached_tokens": 3840},
        },
    }

    stats = extract_cache_stats(response_json)

    assert stats == {
        "cached_tokens": 3840,
        "prompt_tokens": 4387,
        "cache_hit_pct": 87.5,
    }
    assert isinstance(stats, dict)
    assert stats["cached_tokens"] == 3840
    assert stats["prompt_tokens"] == 4387
    assert stats["cache_hit_pct"] == 87.5


def test_extract_cache_stats_rounds_to_one_decimal() -> None:
    """The ratio is rounded to one decimal, matching the logged percentage."""
    # 1 / 3 == 33.333… % -> 33.3 %
    assert extract_cache_stats(
        {"usage": {"prompt_tokens": 3, "prompt_tokens_details": {"cached_tokens": 1}}}
    ) == {"cached_tokens": 1, "prompt_tokens": 3, "cache_hit_pct": 33.3}
    # A full cache hit is exactly 100.0.
    assert extract_cache_stats(
        {"usage": {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 100}}}
    ) == {"cached_tokens": 100, "prompt_tokens": 100, "cache_hit_pct": 100.0}
    # Zero cached tokens is a real 0.0 %, not a missing statistic.
    assert extract_cache_stats(
        {"usage": {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 0}}}
    ) == {"cached_tokens": 0, "prompt_tokens": 100, "cache_hit_pct": 0.0}


@pytest.mark.parametrize(
    "response_json",
    [
        {},  # no usage at all
        {"usage": None},
        {"usage": []},
        {"usage": {"prompt_tokens": 4387}},  # no prompt_tokens_details
        {"usage": {"prompt_tokens_details": {"cached_tokens": 3840}}},  # no prompt_tokens
        {"usage": {"prompt_tokens": 0, "prompt_tokens_details": {"cached_tokens": 0}}},
        {"usage": {"prompt_tokens": -5, "prompt_tokens_details": {"cached_tokens": 1}}},
        {"usage": {"prompt_tokens": "4387", "prompt_tokens_details": {"cached_tokens": 3840}}},
        {"usage": {"prompt_tokens": 4387, "prompt_tokens_details": None}},
        {"usage": {"prompt_tokens": 4387, "prompt_tokens_details": {"cached_tokens": "many"}}},
        {"usage": {"prompt_tokens": 4387, "prompt_tokens_details": {"cached_tokens": -1}}},
        {"usage": {"prompt_tokens": True, "prompt_tokens_details": {"cached_tokens": 1}}},
    ],
)
def test_extract_cache_stats_returns_none_for_malformed_input(
    response_json: dict[str, Any],
) -> None:
    """Every malformed shape returns ``None`` instead of raising."""
    assert extract_cache_stats(response_json) is None


def test_format_cache_log_matches_the_spec_example() -> None:
    """The emitted line matches ``[CACHE HIT] N/M tokens (P%) via <provider>``."""
    stats = {"cached_tokens": 3840, "prompt_tokens": 4387, "cache_hit_pct": 87.5}
    assert format_cache_log(stats, "sharellm_primary") == (
        "[CACHE HIT] 3840/4387 tokens (87.5%) via sharellm_primary"
    )


def test_log_cache_stats_emits_the_line(caplog: pytest.LogCaptureFixture) -> None:
    """``log_cache_stats`` emits the documented line at INFO level."""
    stats = {"cached_tokens": 3840, "prompt_tokens": 4387, "cache_hit_pct": 87.5}
    with caplog.at_level(logging.INFO, logger="gateway.adapters.sharellm"):
        log_cache_stats(stats, "sharellm_primary")

    assert caplog.messages == [
        "[CACHE HIT] 3840/4387 tokens (87.5%) via sharellm_primary"
    ]


# --------------------------------------------------------------------------- #
# Required test 4 — anti-422 parameter sanitizer
# --------------------------------------------------------------------------- #


def test_empty_tool_schemas_are_sanitized() -> None:
    """``tools: []`` and ``tool_choice: "none"`` are removed; nothing else is."""
    payload = {
        "model": "gpt-6.1-sol",
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": False,
        "tools": [],
        "tool_choice": "none",
    }

    sanitized = sanitize_request_payload(payload)

    assert sanitized == {
        "model": "gpt-6.1-sol",
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": False,
    }
    assert "tools" not in sanitized
    assert "tool_choice" not in sanitized
    # The caller's payload is not mutated.
    assert payload["tools"] == []
    assert payload["tool_choice"] == "none"


def test_null_tool_fields_are_sanitized() -> None:
    """``tools: null`` / ``tool_choice: null`` are the spec's "null tool fields"."""
    payload = {"model": "m", "messages": [], "tools": None, "tool_choice": None}
    assert sanitize_request_payload(payload) == {"model": "m", "messages": []}


def test_non_empty_tools_and_real_tool_choice_survive() -> None:
    """Real tool usage is user intent and must not be stripped."""
    tools = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
    payload = {
        "model": "m",
        "messages": [],
        "tools": tools,
        "tool_choice": "auto",
    }
    sanitized = sanitize_request_payload(payload)
    assert sanitized["tools"] == tools
    assert sanitized["tool_choice"] == "auto"


def test_non_empty_tools_preserve_tool_choice() -> None:
    """With non-empty tools, ``tool_choice`` is real intent and must survive (F1).

    Regression guard: stripping ``tool_choice: "none"`` while keeping a
    non-empty ``tools`` list would silently fall back to the upstream default
    (``auto``) and re-enable tool calling the client explicitly disabled. This
    case is discriminating — against an unconditional strip, ``tool_choice``
    would be absent and the lookups below would raise ``KeyError``.
    """
    tools = [{"type": "function", "function": {"name": "f", "parameters": {}}}]

    # "none" = "do not call any tool"; None = "use the provider default" — both
    # are meaningful once tools are actually declared.
    for tool_choice in ("none", None):
        payload: dict[str, Any] = {
            "model": "m",
            "messages": [],
            "tools": tools,
            "tool_choice": tool_choice,
        }

        sanitized = sanitize_request_payload(payload)

        assert sanitized["tools"] == tools
        assert sanitized["tool_choice"] == tool_choice
        assert sanitized == payload


def test_stream_options_rules() -> None:
    """``stream_options`` survives only on a streaming request as a JSON object."""
    stream_options = {"include_usage": True}
    base: dict[str, Any] = {"model": "m", "messages": [], "stream_options": stream_options}

    # Dropped when the request is not streaming (absent or explicit False).
    assert "stream_options" not in sanitize_request_payload(dict(base))
    assert "stream_options" not in sanitize_request_payload({**base, "stream": False})
    # Kept when streaming and the value is a JSON object.
    kept = sanitize_request_payload({**base, "stream": True})
    assert kept["stream_options"] == stream_options
    # Dropped when the value is not a JSON object, even while streaming.
    assert "stream_options" not in sanitize_request_payload(
        {**base, "stream": True, "stream_options": "yes"}
    )
    assert "stream_options" not in sanitize_request_payload(
        {**base, "stream": True, "stream_options": []}
    )


def test_payload_without_tool_fields_is_returned_unchanged() -> None:
    """A payload with no targeted fields survives sanitization byte-identical."""
    payload = {
        "model": "gpt-6.1-sol",
        "messages": [{"role": "user", "content": "Hi"}],
        "temperature": 0.2,
        "top_p": 0.9,
        "stream": True,
    }

    sanitized = sanitize_request_payload(payload)

    assert json.dumps(sanitized) == json.dumps(payload)
    assert sanitized == payload
    # Unchanged means *the same object*: no copy was allocated and nothing was
    # reordered (F6).
    assert sanitized is payload
    assert sanitize_request_payload(payload) is payload


# --------------------------------------------------------------------------- #
# Integration — normalization and cache logging through the ASGI app
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_upstream_share_llm_error_is_normalized_end_to_end(
    gateway: GatewayHarness,
) -> None:
    """A non-retryable ShareLLM error is normalized before it reaches the client."""
    upstream_error = {
        "error": {
            "message": "Unknown field 'foo'",
            "type": SHARE_LLM_ERROR_TYPE,
            "code": "invalid_field",
        }
    }

    with respx.mock(assert_all_called=False) as mock:
        mock.post(PRIMARY_URL).mock(
            return_value=httpx.Response(
                400,
                json=upstream_error,
                headers={"content-type": "application/json"},
            )
        )

        response = await gateway.client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-6.1-sol",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": False,
            },
        )

    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "message": "Unknown field 'foo'",
            "type": "invalid_request_error",
            "code": "invalid_field",
        }
    }


@pytest.mark.asyncio
async def test_failover_exhausted_envelope_is_not_rewritten(
    gateway: GatewayHarness,
) -> None:
    """Every target 429 → the gateway's own envelope is returned untouched."""
    with respx.mock(assert_all_called=False) as mock:
        mock.post(PRIMARY_URL).mock(
            return_value=httpx.Response(
                429,
                json={"error": {"message": "primary rate limited", "type": SHARE_LLM_ERROR_TYPE}},
            )
        )
        mock.post(BACKUP_URL).mock(
            return_value=httpx.Response(
                429,
                json={"error": {"message": "backup rate limited", "type": SHARE_LLM_ERROR_TYPE}},
            )
        )

        response = await gateway.client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-6.1-sol",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": False,
            },
        )

    assert response.status_code == 429
    assert response.json() == {
        "error": {
            "message": "All upstream targets failed",
            "type": "gateway_failover_exhausted",
        }
    }


@pytest.mark.asyncio
async def test_cache_hit_is_logged_with_the_serving_provider(
    gateway: GatewayHarness,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-streaming success emits the cache line naming the serving provider."""
    upstream_body: dict[str, Any] = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": "gpt-6.1-sol",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 4387,
            "completion_tokens": 12,
            "total_tokens": 4399,
            "prompt_tokens_details": {"cached_tokens": 3840},
        },
    }

    with caplog.at_level(logging.INFO, logger="gateway.adapters.sharellm"):
        with respx.mock(assert_all_called=False) as mock:
            mock.post(PRIMARY_URL).mock(
                return_value=httpx.Response(
                    200,
                    json=upstream_body,
                    headers={"content-type": "application/json"},
                )
            )

            response = await gateway.client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-6.1-sol",
                    "messages": [{"role": "user", "content": "Hi"}],
                    "stream": False,
                },
            )

    assert response.status_code == 200
    assert response.json() == upstream_body
    assert (
        "[CACHE HIT] 3840/4387 tokens (87.5%) via sharellm_primary" in caplog.text
    )


@pytest.mark.asyncio
async def test_no_cache_line_without_cache_statistics(
    gateway: GatewayHarness,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A response with no prompt-token details emits no cache line."""
    upstream_body: dict[str, Any] = {
        "id": "chatcmpl-2",
        "object": "chat.completion",
        "model": "gpt-6.1-sol",
        "choices": [],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
    }

    with caplog.at_level(logging.INFO, logger="gateway.adapters.sharellm"):
        with respx.mock(assert_all_called=False) as mock:
            mock.post(PRIMARY_URL).mock(
                return_value=httpx.Response(
                    200,
                    json=upstream_body,
                    headers={"content-type": "application/json"},
                )
            )

            response = await gateway.client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-6.1-sol",
                    "messages": [{"role": "user", "content": "Hi"}],
                    "stream": False,
                },
            )

    assert response.status_code == 200
    assert "[CACHE HIT]" not in caplog.text


@pytest.mark.asyncio
async def test_sanitized_payload_reaches_the_upstream(
    gateway: GatewayHarness,
) -> None:
    """The empty tool fields never reach the upstream (the 422 guard, end to end)."""
    with respx.mock(assert_all_called=False) as mock:
        primary = mock.post(PRIMARY_URL).mock(
            return_value=httpx.Response(200, json=BACKUP_SUCCESS_BODY)
        )

        response = await gateway.client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-6.1-sol",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": False,
                "tools": [],
                "tool_choice": "none",
            },
        )

        forwarded = json.loads(primary.calls.last.request.content)

    assert response.status_code == 200
    assert forwarded == {
        "model": "gpt-6.1-sol",
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": False,
    }
