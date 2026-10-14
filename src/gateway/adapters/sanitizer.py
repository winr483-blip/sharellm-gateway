"""Anti-422 request-parameter sanitizer.

Strict OpenAI-compatible upstreams reject requests that carry *present but
empty* tool parameters — ``"tools": []``, or a ``"tool_choice": "none"`` that
selects nothing — with ``422 Unprocessable Entity``, even though those values
are valid for the OpenAI API and mean "no tools". The same is true of a
``stream_options`` object on a request that is not streaming, or one whose
value is not an object at all.

This module removes exactly those no-op fields so a request the gateway
forwards is never rejected for a field that carries no user intent.

The rule is deliberately conservative, and one field is subtler than it looks:
``tool_choice`` is removed **only when there are no tools to choose from**. With
a non-empty ``tools`` list, ``"tool_choice": "none"`` is a real instruction —
"do not call any tool" — that a transparent gateway must not silently drop, so
it is preserved.

**Only** the fields listed in :func:`sanitize_request_payload` are touched.
Every other member — including any field the spec did not name — is preserved,
and a payload that carries none of the targeted fields is returned unchanged
(the caller's own object, never a mutated copy).
"""

from __future__ import annotations

from typing import Any

__all__ = ["sanitize_request_payload"]


def _has_declared_tools(payload: dict[str, Any]) -> bool:
    """Return whether ``payload`` declares a usable (non-empty) tool list."""
    tools = payload.get("tools")
    return isinstance(tools, list) and len(tools) > 0


def sanitize_request_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return ``payload`` with unsupported empty / null tool fields removed.

    The spec asks to "clean the payload of unsupported null/empty tool fields",
    so exactly these are removed:

    * ``tools`` when it is ``None`` or an empty list;
    * ``tool_choice`` when it is ``None`` or the string ``"none"`` **and** the
      request declares no non-empty ``tools`` list — i.e. only when the choice
      is a no-op. When ``tools`` is a non-empty list, ``tool_choice`` is
      preserved whatever its value, because dropping it would silently turn an
      explicit "do not call any tool" into the upstream's default (``auto``);
    * ``stream_options`` when the request is not streaming (``stream`` is
      absent or falsy) **or** when its value is not a JSON object.

    Nothing else is stripped: a non-empty ``tools`` list, a ``tool_choice`` of
    ``"auto"`` / ``"required"`` / a specific function, and every unrelated field
    survive untouched. A payload that carries none of the targeted fields is
    returned unchanged (byte-identical, same object).

    The input mapping is never mutated.

    Args:
        payload: The decoded OpenAI-style request body.

    Returns:
        A new mapping without the removed fields, or ``payload`` itself when
        nothing needed removing.
    """
    sanitized = dict(payload)
    changed = False

    if "tools" in sanitized:
        tools = sanitized["tools"]
        if tools is None or (isinstance(tools, list) and not tools):
            del sanitized["tools"]
            changed = True

    # `tool_choice` is a no-op only when there is nothing to choose from. With a
    # non-empty `tools` list, `"none"` means "do not call any tool" and `None`
    # means "use the provider default" — both carry intent and are preserved.
    if "tool_choice" in sanitized and not _has_declared_tools(sanitized):
        tool_choice = sanitized["tool_choice"]
        if tool_choice is None or tool_choice == "none":
            del sanitized["tool_choice"]
            changed = True

    if "stream_options" in sanitized:
        stream_options = sanitized["stream_options"]
        is_streaming = bool(sanitized.get("stream", False))
        if not is_streaming or not isinstance(stream_options, dict):
            del sanitized["stream_options"]
            changed = True

    return sanitized if changed else payload
