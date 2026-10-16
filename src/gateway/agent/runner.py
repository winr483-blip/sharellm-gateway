"""Bounded, non-streaming OpenAI tool-call coordinator.

The caller owns the HTTP client and ToolRegistry. No sockets, files or tasks are
created on import. Tool results are untrusted data, not system instructions.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field

from gateway.agent.tools import ToolRegistry

MAX_STEPS = 15
MAX_TOOL_CALLS = 8
MAX_TOOL_OUTPUT = 65_536
MAX_RESPONSE_BYTES = 2_097_152
MAX_MESSAGES = 512


class TrajectoryStep(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    step: int
    tool_call_id: str
    tool_name: str
    arguments: str
    result: str
    success: bool
    duration_ms: float


class AgentSession(BaseModel):
    """Process-local conversation; each session serializes its own turns."""
    model_config = ConfigDict(strict=True, extra="forbid")
    session_id: str = Field(default_factory=lambda: str(uuid4()))
    messages: list[dict[str, Any]] = Field(default_factory=list)
    trajectory: list[TrajectoryStep] = Field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    steps_taken: int = 0
    status: Literal["idle", "running", "completed", "limit_reached", "failed", "cancelled"] = "idle"


class AgentRunner:
    """One model request per step, at most eight sequential calls per step.

    ``client`` must point to the trusted gateway (or be an ASGI test client).
    Monetary cost is not invented: only reported token usage is accumulated.
    """

    def __init__(
        self, registry: ToolRegistry, client: httpx.AsyncClient,
        model: str, *, request_timeout: float = 60.0,
    ) -> None:
        if not model.strip():
            raise ValueError("model must not be empty")
        if not 0 < request_timeout <= 300:
            raise ValueError("request_timeout must be in (0, 300]")
        self.registry = registry
        self.client = client
        self.model = model
        self.request_timeout = request_timeout
        # Fixed stripes bound synchronization state without a growing session map.
        self._locks = [asyncio.Lock() for _ in range(32)]

    @staticmethod
    def _answer(session: AgentSession, text: str) -> str:
        session.messages.append({"role": "assistant", "content": text})
        return text

    @staticmethod
    def _usage(session: AgentSession, body: dict[str, Any]) -> None:
        usage = body.get("usage")
        if not isinstance(usage, dict):
            return
        def count(key: str) -> int:
            value = usage.get(key)
            return value if type(value) is int and value >= 0 else 0
        prompt, completion = count("prompt_tokens"), count("completion_tokens")
        session.prompt_tokens += prompt
        session.completion_tokens += completion
        total = usage.get("total_tokens")
        session.total_tokens += total if type(total) is int and total >= 0 else prompt + completion

    async def run_turn(
        self, session: AgentSession, prompt: str, max_steps: int = 10,
    ) -> str:
        if type(max_steps) is not int or not 1 <= max_steps <= MAX_STEPS:
            raise ValueError("max_steps must be an integer between 1 and 15")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 65_536:
            raise ValueError("prompt must contain 1..65536 characters")
        async with self._locks[hash(session.session_id) % len(self._locks)]:
            if session.status == "cancelled":
                raise ValueError("cancelled session cannot be resumed with incomplete tool calls")
            if len(session.messages) + 1 + max_steps * (MAX_TOOL_CALLS + 1) + 1 > MAX_MESSAGES:
                raise ValueError("session conversation capacity reached")
            session.messages.append({"role": "user", "content": prompt})
            session.status = "running"
            session.steps_taken = 0
            try:
                return await self._run(session, max_steps)
            except asyncio.CancelledError:
                session.status = "cancelled"
                raise
            except Exception:
                session.status = "failed"
                return self._answer(session, "Agent turn failed: gateway response unavailable or invalid.")

    async def _run(self, session: AgentSession, max_steps: int) -> str:
        definitions = [tool.model_dump(mode="json") for tool in self.registry.definitions]
        for step in range(1, max_steps + 1):
            response = await self.client.post(
                "/v1/chat/completions",
                json={"model": self.model, "messages": session.messages,
                      "tools": definitions, "stream": False},
                timeout=self.request_timeout,
            )
            response.raise_for_status()
            if len(response.content) > MAX_RESPONSE_BYTES:
                raise ValueError("model response exceeds size limit")
            body: Any = response.json()
            if not isinstance(body, dict):
                raise ValueError("model response must be an object")
            choices = body.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise ValueError("missing response choice")
            message = choices[0].get("message")
            if not isinstance(message, dict):
                raise ValueError("missing assistant message")
            content = message.get("content")
            if content is not None and not isinstance(content, str):
                raise ValueError("assistant content must be text or null")
            raw_calls = message.get("tool_calls")
            if raw_calls is not None and not isinstance(raw_calls, list):
                raise ValueError("tool_calls must be an array")
            calls = raw_calls or []
            if len(calls) > MAX_TOOL_CALLS:
                raise ValueError("too many tool calls in one step")
            validated: list[dict[str, Any]] = []
            ids: set[str] = set()
            for call in calls:
                if not isinstance(call, dict) or call.get("type") != "function":
                    raise ValueError("invalid tool call")
                call_id, function = call.get("id"), call.get("function")
                if not isinstance(call_id, str) or not call_id or call_id in ids:
                    raise ValueError("tool call IDs must be nonempty and unique")
                if not isinstance(function, dict):
                    raise ValueError("missing tool function")
                name, arguments = function.get("name"), function.get("arguments")
                if not isinstance(name, str) or not isinstance(arguments, str) or len(arguments) > 1_048_576:
                    raise ValueError("invalid function name or arguments")
                ids.add(call_id)
                validated.append({"id": call_id, "type": "function",
                                  "function": {"name": name, "arguments": arguments}})
            self._usage(session, body)
            session.steps_taken = step
            if not validated:
                session.status = "completed"
                return self._answer(session, content or "")
            session.messages.append({"role": "assistant", "content": content,
                                     "tool_calls": validated})
            for call in validated:
                name = call["function"]["name"]
                arguments = call["function"]["arguments"]
                started = time.monotonic()
                success = True
                try:
                    result = await self.registry.dispatch(name, arguments)
                    rendered = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, allow_nan=False)
                except Exception as exc:
                    success = False
                    # Exceptions may contain file content or credentials; do not echo them.
                    rendered = "Tool error (" + type(exc).__name__ + "): execution failed or arguments were rejected."
                if len(rendered) > MAX_TOOL_OUTPUT:
                    rendered = rendered[:MAX_TOOL_OUTPUT] + "\n[tool output truncated]"
                session.messages.append({"role": "tool", "tool_call_id": call["id"], "content": rendered})
                session.trajectory.append(TrajectoryStep(
                    step=step, tool_call_id=call["id"], tool_name=name,
                    arguments=arguments, result=rendered, success=success,
                    duration_ms=max(0.0, (time.monotonic() - started) * 1000),
                ))
        session.status = "limit_reached"
        return self._answer(session, f"Agent stopped: step limit reached ({max_steps}).")
