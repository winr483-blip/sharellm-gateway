"""ShareLLM provider adapter: error normalization and prompt-cache telemetry.

ShareLLM upstreams speak the OpenAI chat-completions dialect with two
ShareLLM-specific deviations that this adapter normalizes for the rest of the
gateway:

* **Errors** arrive with a non-standard ``type: "share_llm_error"``. A client
  written against the OpenAI API must never see that type, so
  :func:`sanitize_error_envelope` maps it onto the standard OpenAI error types
  (``rate_limit_error`` / ``invalid_request_error`` / ``api_error``) while
  preserving the upstream ``message`` and ``code``.
* **Prompt caching** is reported under
  ``usage.prompt_tokens_details.cached_tokens``. :func:`extract_cache_stats`
  turns that into a cache-hit ratio and :func:`log_cache_stats` emits the
  gateway's structured ``[CACHE HIT] …`` line.

Design note — why :func:`extract_cache_stats` is pure
-----------------------------------------------------

The task spec pins ``extract_cache_stats(response_json: dict) -> dict | None``
*and* describes a log line that ends ``via <provider>``. A response body alone
cannot name the provider, so the two concerns are split: the extractor is a
**pure** function that returns the statistics (or ``None``) and performs no
I/O, and the ``[CACHE HIT] … via <provider>`` line is emitted by the caller
(:mod:`gateway.main`) that knows which provider served the response. Keeping
the extractor pure makes it exhaustively testable without capturing logs.
"""

from __future__ import annotations

import logging
import re
from typing import Any, TypedDict

__all__ = [
    "CacheStats",
    "SHARE_LLM_ERROR_TYPE",
    "extract_cache_stats",
    "format_cache_log",
    "log_cache_stats",
    "sanitize_error_envelope",
]

logger = logging.getLogger(__name__)

#: The non-standard error ``type`` emitted by ShareLLM upstreams.
SHARE_LLM_ERROR_TYPE = "share_llm_error"

#: Standard OpenAI error types a ShareLLM error is normalized onto.
_RATE_LIMIT_ERROR = "rate_limit_error"
_INVALID_REQUEST_ERROR = "invalid_request_error"
_API_ERROR = "api_error"

#: Matches a rate-limit / overload keyword as a **whole word**, case-
#: insensitively. The word boundaries (``\b``) are the point: an unanchored
#: ``"limit"`` substring would classify an unrelated 400 whose message merely
#: contains those letters — e.g. ``"unlimited tokens available"`` — as a
#: rate-limit error, masking the real ``invalid_request_error``. A genuine
#: ``"rate limit exceeded"`` still matches, because ``limit`` is its own word.
_RATE_LIMIT_PATTERN = re.compile(r"\b(limit|limits|overloaded)\b", re.IGNORECASE)

#: Lower bound (inclusive) and upper bound (exclusive) of the HTTP 5xx range.
_SERVER_ERROR_MIN = 500
_SERVER_ERROR_MAX = 600


class CacheStats(TypedDict):
    """Prompt-cache statistics extracted from an upstream response body.

    A concrete, self-describing shape (rather than a bare ``dict``) so the
    logged line and the returned value cannot drift apart:

    * ``cached_tokens`` — tokens served from the provider's prompt cache.
    * ``prompt_tokens`` — total prompt tokens for the request.
    * ``cache_hit_pct`` — ``cached_tokens / prompt_tokens * 100``, rounded to
      one decimal place (matching the spec's ``87.5%`` example).
    """

    cached_tokens: int
    prompt_tokens: int
    cache_hit_pct: float


def _as_number(value: object) -> int | float | None:
    """Return ``value`` when it is a real number, else ``None``.

    ``bool`` is rejected explicitly: it is an ``int`` subclass in Python, but
    ``True`` / ``False`` are not valid token counts and would silently poison
    the ratio if accepted.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _normalized_error_type(status_code: int, message: str) -> str | None:
    """Return the standard OpenAI error type for a ShareLLM error, or ``None``.

    ``None`` means the status is one the spec does not map (e.g. 401/403/404/
    422); the caller then leaves the upstream ``type`` untouched rather than
    inventing a mapping the spec did not ask for.

    Precedence (highest first): a rate-limit status or a whole-word ``"limit"``
    / ``"limits"`` / ``"overloaded"`` message, then 400, then any other 5xx.
    """
    if status_code == 429 or _RATE_LIMIT_PATTERN.search(message) is not None:
        return _RATE_LIMIT_ERROR
    if status_code == 400:
        return _INVALID_REQUEST_ERROR
    if _SERVER_ERROR_MIN <= status_code < _SERVER_ERROR_MAX:
        return _API_ERROR
    return None


def sanitize_error_envelope(status_code: int, error_json: dict[str, Any]) -> dict[str, Any]:
    """Normalize a ShareLLM error envelope onto the standard OpenAI types.

    Only an envelope whose ``error.type`` is exactly
    :data:`SHARE_LLM_ERROR_TYPE` is touched, and only when the status is one
    the spec covers (429 / 400 / 5xx, or any status carrying a rate-limit
    message). Everything else — a missing or non-mapping ``error`` member, a
    different ``type``, or a ShareLLM error on an unmapped status — is returned
    **unchanged**, because the gateway must not invent error mappings the spec
    did not ask for.

    The upstream ``message`` and ``code`` are always preserved verbatim (and
    never invented when absent). The input mapping is not mutated.

    Args:
        status_code: The HTTP status the upstream error arrived with.
        error_json: The decoded upstream error body, e.g.
            ``{"error": {"message": ..., "type": ..., "code": ...}}``.

    Returns:
        A normalized error envelope, or ``error_json`` itself when no
        normalization applies.
    """
    error = error_json.get("error")
    if not isinstance(error, dict) or error.get("type") != SHARE_LLM_ERROR_TYPE:
        return error_json

    message = error.get("message")
    normalized = _normalized_error_type(
        status_code, message if isinstance(message, str) else ""
    )
    if normalized is None:
        return error_json

    # A shallow copy preserves every other member (``message``, ``code``,
    # ``param``, provider extras) and leaves the caller's mapping untouched.
    sanitized_error = dict(error)
    sanitized_error["type"] = normalized
    return {**error_json, "error": sanitized_error}


def extract_cache_stats(response_json: dict[str, Any]) -> CacheStats | None:
    """Extract prompt-cache statistics from an upstream response body.

    Reads ``usage.prompt_tokens_details.cached_tokens`` and
    ``usage.prompt_tokens`` and computes
    ``cached_tokens / prompt_tokens * 100``, rounded to one decimal place.

    This function is pure: it performs no logging and has no side effects.

    Args:
        response_json: A decoded chat-completions response body.

    Returns:
        ``{"cached_tokens": int, "prompt_tokens": int, "cache_hit_pct": float}``
        when the body carries usable cache statistics, otherwise ``None``.
        ``None`` is returned (rather than raising) for every malformed shape:
        a missing / non-mapping ``usage``, a missing / non-numeric
        ``prompt_tokens``, a non-positive ``prompt_tokens`` (division by zero),
        a missing / non-mapping ``prompt_tokens_details``, and a missing /
        non-numeric / negative ``cached_tokens``.
    """
    usage = response_json.get("usage")
    if not isinstance(usage, dict):
        return None

    prompt_tokens = _as_number(usage.get("prompt_tokens"))
    if prompt_tokens is None or prompt_tokens <= 0:
        return None

    details = usage.get("prompt_tokens_details")
    if not isinstance(details, dict):
        return None

    cached_tokens = _as_number(details.get("cached_tokens"))
    if cached_tokens is None or cached_tokens < 0:
        return None

    cache_hit_pct = round(cached_tokens / prompt_tokens * 100.0, 1)
    return {
        "cached_tokens": int(cached_tokens),
        "prompt_tokens": int(prompt_tokens),
        "cache_hit_pct": cache_hit_pct,
    }


def format_cache_log(stats: CacheStats, provider: str) -> str:
    """Render the structured cache-hit log line for ``provider``.

    The percentage is rendered from the already-rounded ``cache_hit_pct``, so
    the emitted line and the value returned by :func:`extract_cache_stats` can
    never disagree. The format matches the spec's example:
    ``[CACHE HIT] 3840/4387 tokens (87.5%) via <provider>``.
    """
    return (
        f"[CACHE HIT] {stats['cached_tokens']}/{stats['prompt_tokens']} tokens "
        f"({stats['cache_hit_pct']:.1f}%) via {provider}"
    )


def log_cache_stats(stats: CacheStats, provider: str) -> None:
    """Emit the structured ``[CACHE HIT] …`` line at INFO level."""
    logger.info(format_cache_log(stats, provider))
